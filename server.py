from datetime import timedelta

import arrow
import flask_profiler
import os
import sentry_sdk
import ssl
from flask import (
    Flask,
    redirect,
    url_for,
    render_template,
    request,
    jsonify,
    flash,
    session,
)
from flask_admin import Admin
from flask_cors import cross_origin, CORS
from flask_login import current_user
from sentry_sdk.integrations.aiohttp import AioHttpIntegration
from sentry_sdk.integrations.flask import FlaskIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from werkzeug.middleware.proxy_fix import ProxyFix

from app import paddle_utils
from app.admin_model import SLModelView, SLAdminIndexView
from app.api.base import api_bp
from app.auth.base import auth_bp
from app.config import (
    DB_URI,
    FLASK_SECRET,
    SENTRY_DSN,
    URL,
    SHA1,
    PADDLE_MONTHLY_PRODUCT_ID,
    RESET_DB,
    FLASK_PROFILER_PATH,
    FLASK_PROFILER_PASSWORD,
    SENTRY_FRONT_END_DSN,
    FIRST_ALIAS_DOMAIN,
    SESSION_COOKIE_NAME,
)
from app.dashboard.base import dashboard_bp
from app.developer.base import developer_bp
from app.discover.base import discover_bp
from app.extensions import db, login_manager, migrate, limiter
from app.jose_utils import get_jwk_key
from app.log import LOG
from app.models import (
    Client,
    User,
    ClientUser,
    Alias,
    RedirectUri,
    Subscription,
    PlanEnum,
    ApiKey,
    CustomDomain,
    LifetimeCoupon,
    Directory,
    Mailbox,
    DeletedAlias,
    Referral,
    AliasMailbox,
    Notification,
)
from app.monitor.base import monitor_bp
from app.oauth.base import oauth_bp

if SENTRY_DSN:
    LOG.d("enable sentry")
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[
            FlaskIntegration(),
            SqlalchemyIntegration(),
            AioHttpIntegration(),
        ],
    )

# the app is served behin nginx which uses http and not https
os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"


def create_app() -> Flask:
    app = Flask(__name__)
    # SimpleLogin is deployed behind NGINX
    app.wsgi_app = ProxyFix(app.wsgi_app, num_proxies=1)
    limiter.init_app(app)

    app.url_map.strict_slashes = False

    app.config["SQLALCHEMY_DATABASE_URI"] = DB_URI
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    # enable to print all queries generated by sqlalchemy
    # app.config["SQLALCHEMY_ECHO"] = True

    app.secret_key = FLASK_SECRET

    app.config["TEMPLATES_AUTO_RELOAD"] = True

    # to avoid conflict with other cookie
    app.config["SESSION_COOKIE_NAME"] = SESSION_COOKIE_NAME
    if URL.startswith("https"):
        app.config["SESSION_COOKIE_SECURE"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    setup_error_page(app)

    init_extensions(app)
    register_blueprints(app)
    set_index_page(app)
    jinja2_filter(app)

    setup_favicon_route(app)
    setup_openid_metadata(app)

    init_admin(app)
    setup_paddle_callback(app)
    setup_do_not_track(app)

    if FLASK_PROFILER_PATH:
        LOG.d("Enable flask-profiler")
        app.config["flask_profiler"] = {
            "enabled": True,
            "storage": {"engine": "sqlite", "FILE": FLASK_PROFILER_PATH},
            "basicAuth": {
                "enabled": True,
                "username": "admin",
                "password": FLASK_PROFILER_PASSWORD,
            },
            "ignore": ["^/static/.*", "/git", "/exception"],
        }
        flask_profiler.init_app(app)

    # enable CORS on /api endpoints
    cors = CORS(app, resources={r"/api/*": {"origins": "*"}})

    # set session to permanent so user stays signed in after quitting the browser
    # the cookie is valid for 7 days
    @app.before_request
    def make_session_permanent():
        session.permanent = True
        app.permanent_session_lifetime = timedelta(days=7)

    return app


def fake_data():
    LOG.d("create fake data")
    # Remove db if exist
    if os.path.exists("db.sqlite"):
        LOG.d("remove existing db file")
        os.remove("db.sqlite")

    # Create all tables
    db.create_all()

    # Create a user
    user = User.create(
        email="john@wick.com",
        name="John Wick",
        password="password",
        activated=True,
        is_admin=True,
        otp_secret="base32secret3232",
        intro_shown=True,
        fido_uuid=None,
    )
    db.session.commit()

    user.trial_end = None

    LifetimeCoupon.create(code="coupon", nb_used=10)
    db.session.commit()

    # Create a subscription for user
    Subscription.create(
        user_id=user.id,
        cancel_url="https://checkout.paddle.com/subscription/cancel?user=1234",
        update_url="https://checkout.paddle.com/subscription/update?user=1234",
        subscription_id="123",
        event_time=arrow.now(),
        next_bill_date=arrow.now().shift(days=10).date(),
        plan=PlanEnum.monthly,
    )
    db.session.commit()

    api_key = ApiKey.create(user_id=user.id, name="Chrome")
    api_key.code = "code"

    api_key = ApiKey.create(user_id=user.id, name="Firefox")
    api_key.code = "codeFF"

    m1 = Mailbox.create(
        user_id=user.id,
        email="m1@cd.ef",
        verified=True,
        pgp_finger_print="fake fingerprint",
    )
    db.session.commit()

    for i in range(31):
        if i % 2 == 0:
            a = Alias.create(
                email=f"e{i}@{FIRST_ALIAS_DOMAIN}", user_id=user.id, mailbox_id=m1.id
            )
        else:
            a = Alias.create(
                email=f"e{i}@{FIRST_ALIAS_DOMAIN}",
                user_id=user.id,
                mailbox_id=user.default_mailbox_id,
            )
        db.session.commit()

        if i % 5 == 0:
            if i % 2 == 0:
                AliasMailbox.create(alias_id=a.id, mailbox_id=user.default_mailbox_id)
            else:
                AliasMailbox.create(alias_id=a.id, mailbox_id=m1.id)
        db.session.commit()

        # some aliases don't have any activity
        # if i % 3 != 0:
        #     contact = Contact.create(
        #         user_id=user.id,
        #         alias_id=a.id,
        #         website_email=f"contact{i}@example.com",
        #         reply_email=f"rep{i}@sl.local",
        #     )
        #     db.session.commit()
        #     for _ in range(3):
        #         EmailLog.create(user_id=user.id, contact_id=contact.id)
        #         db.session.commit()

        # have some disabled alias
        if i % 5 == 0:
            a.enabled = False
            db.session.commit()

    CustomDomain.create(user_id=user.id, domain="ab.cd", verified=True)
    CustomDomain.create(
        user_id=user.id, domain="very-long-domain.com.net.org", verified=True
    )
    db.session.commit()

    Directory.create(user_id=user.id, name="abcd")
    Directory.create(user_id=user.id, name="xyzt")
    db.session.commit()

    # Create a client
    client1 = Client.create_new(name="Demo", user_id=user.id)
    client1.oauth_client_id = "client-id"
    client1.oauth_client_secret = "client-secret"
    client1.published = True
    db.session.commit()

    RedirectUri.create(client_id=client1.id, uri="https://ab.com")

    client2 = Client.create_new(name="Demo 2", user_id=user.id)
    client2.oauth_client_id = "client-id2"
    client2.oauth_client_secret = "client-secret2"
    client2.published = True
    db.session.commit()

    ClientUser.create(user_id=user.id, client_id=client1.id, name="Fake Name")

    DeletedAlias.create(email="d1@ab.cd")
    DeletedAlias.create(email="d2@ab.cd")
    db.session.commit()

    referral = Referral.create(user_id=user.id, code="REFCODE", name="First referral")
    db.session.commit()

    for i in range(6):
        Notification.create(user_id=user.id, message=f"""Hey hey <b>{i}</b> """ * 10)
    db.session.commit()

    User.create(
        email="winston@continental.com",
        name="Winston",
        password="password",
        activated=True,
        referral_id=referral.id,
    )
    db.session.commit()


@login_manager.user_loader
def load_user(user_id):
    user = User.query.get(user_id)

    return user


def register_blueprints(app: Flask):
    app.register_blueprint(auth_bp)
    app.register_blueprint(monitor_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(developer_bp)

    app.register_blueprint(oauth_bp, url_prefix="/oauth")
    app.register_blueprint(oauth_bp, url_prefix="/oauth2")

    app.register_blueprint(discover_bp)
    app.register_blueprint(api_bp)


def set_index_page(app):
    @app.route("/", methods=["GET", "POST"])
    def index():
        if current_user.is_authenticated:
            return redirect(url_for("dashboard.index"))
        else:
            return redirect(url_for("auth.login"))

    @app.after_request
    def after_request(res):
        # not logging /static call
        if (
            not request.path.startswith("/static")
            and not request.path.startswith("/admin/static")
            and not request.path.startswith("/_debug_toolbar")
        ):
            LOG.debug(
                "%s %s %s %s %s",
                request.remote_addr,
                request.method,
                request.path,
                request.args,
                res.status_code,
            )

        return res


def setup_openid_metadata(app):
    @app.route("/.well-known/openid-configuration")
    @cross_origin()
    def openid_config():
        res = {
            "issuer": URL,
            "authorization_endpoint": URL + "/oauth2/authorize",
            "token_endpoint": URL + "/oauth2/token",
            "userinfo_endpoint": URL + "/oauth2/userinfo",
            "jwks_uri": URL + "/jwks",
            "response_types_supported": [
                "code",
                "token",
                "id_token",
                "id_token token",
                "id_token code",
            ],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            # todo: add introspection and revocation endpoints
            # "introspection_endpoint": URL + "/oauth2/token/introspection",
            # "revocation_endpoint": URL + "/oauth2/token/revocation",
        }

        return jsonify(res)

    @app.route("/jwks")
    @cross_origin()
    def jwks():
        res = {"keys": [get_jwk_key()]}
        return jsonify(res)


def setup_error_page(app):
    @app.errorhandler(400)
    def bad_request(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Bad Request"), 400
        else:
            return render_template("error/400.html"), 400

    @app.errorhandler(401)
    def unauthorized(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Unauthorized"), 401
        else:
            flash("You need to login to see this page", "error")
            return redirect(url_for("auth.login", next=request.full_path))

    @app.errorhandler(403)
    def forbidden(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Forbidden"), 403
        else:
            return render_template("error/403.html"), 403

    @app.errorhandler(429)
    def forbidden(e):
        LOG.error("Client hit rate limit on path %s", request.path)
        if request.path.startswith("/api/"):
            return jsonify(error="Rate limit exceeded"), 429
        else:
            return render_template("error/429.html"), 429

    @app.errorhandler(404)
    def page_not_found(e):
        if request.path.startswith("/api/"):
            return jsonify(error="No such endpoint"), 404
        else:
            return render_template("error/404.html"), 404

    @app.errorhandler(405)
    def wrong_method(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Method not allowed"), 405
        else:
            return render_template("error/405.html"), 405

    @app.errorhandler(Exception)
    def error_handler(e):
        LOG.exception(e)
        if request.path.startswith("/api/"):
            return jsonify(error="Internal error"), 500
        else:
            return render_template("error/500.html"), 500


def setup_favicon_route(app):
    @app.route("/favicon.ico")
    def favicon():
        return redirect("/static/favicon.ico")


def jinja2_filter(app):
    def format_datetime(value):
        dt = arrow.get(value)
        return dt.humanize()

    app.jinja_env.filters["dt"] = format_datetime

    @app.context_processor
    def inject_stage_and_region():
        return dict(
            YEAR=arrow.now().year,
            URL=URL,
            SENTRY_DSN=SENTRY_FRONT_END_DSN,
            VERSION=SHA1,
            FIRST_ALIAS_DOMAIN=FIRST_ALIAS_DOMAIN,
        )


def setup_paddle_callback(app: Flask):
    @app.route("/paddle", methods=["GET", "POST"])
    def paddle():
        LOG.debug(f"paddle callback {request.form.get('alert_name')} {request.form}")

        # make sure the request comes from Paddle
        if not paddle_utils.verify_incoming_request(dict(request.form)):
            LOG.error(
                "request not coming from paddle. Request data:%s", dict(request.form)
            )
            return "KO", 400

        if (
            request.form.get("alert_name") == "subscription_created"
        ):  # new user subscribes
            user_email = request.form.get("email")
            user = User.get_by(email=user_email)

            if (
                int(request.form.get("subscription_plan_id"))
                == PADDLE_MONTHLY_PRODUCT_ID
            ):
                plan = PlanEnum.monthly
            else:
                plan = PlanEnum.yearly

            sub = Subscription.get_by(user_id=user.id)

            if not sub:
                LOG.d(f"create a new Subscription for user {user}")
                Subscription.create(
                    user_id=user.id,
                    cancel_url=request.form.get("cancel_url"),
                    update_url=request.form.get("update_url"),
                    subscription_id=request.form.get("subscription_id"),
                    event_time=arrow.now(),
                    next_bill_date=arrow.get(
                        request.form.get("next_bill_date"), "YYYY-MM-DD"
                    ).date(),
                    plan=plan,
                )
            else:
                LOG.d(f"Update an existing Subscription for user {user}")
                sub.cancel_url = request.form.get("cancel_url")
                sub.update_url = request.form.get("update_url")
                sub.subscription_id = request.form.get("subscription_id")
                sub.event_time = arrow.now()
                sub.next_bill_date = arrow.get(
                    request.form.get("next_bill_date"), "YYYY-MM-DD"
                ).date()
                sub.plan = plan

                # make sure to set the new plan as not-cancelled
                # in case user cancels a plan and subscribes a new plan
                sub.cancelled = False

            LOG.debug("User %s upgrades!", user)

            db.session.commit()

        elif request.form.get("alert_name") == "subscription_payment_succeeded":
            subscription_id = request.form.get("subscription_id")
            LOG.debug("Update subscription %s", subscription_id)

            sub: Subscription = Subscription.get_by(subscription_id=subscription_id)
            # when user subscribes, the "subscription_payment_succeeded" can arrive BEFORE "subscription_created"
            # at that time, subscription object does not exist yet
            if sub:
                sub.event_time = arrow.now()
                sub.next_bill_date = arrow.get(
                    request.form.get("next_bill_date"), "YYYY-MM-DD"
                ).date()

                db.session.commit()

        elif request.form.get("alert_name") == "subscription_cancelled":
            subscription_id = request.form.get("subscription_id")

            sub: Subscription = Subscription.get_by(subscription_id=subscription_id)
            if sub:
                # cancellation_effective_date should be the same as next_bill_date
                LOG.warning(
                    "Cancel subscription %s %s on %s, next bill date %s",
                    subscription_id,
                    sub.user,
                    request.form.get("cancellation_effective_date"),
                    sub.next_bill_date,
                )
                sub.event_time = arrow.now()

                sub.cancelled = True
                db.session.commit()
            else:
                return "No such subscription", 400
        elif request.form.get("alert_name") == "subscription_updated":
            subscription_id = request.form.get("subscription_id")

            sub: Subscription = Subscription.get_by(subscription_id=subscription_id)
            if sub:
                LOG.debug(
                    "Update subscription %s %s on %s, next bill date %s",
                    subscription_id,
                    sub.user,
                    request.form.get("cancellation_effective_date"),
                    sub.next_bill_date,
                )
                if (
                    int(request.form.get("subscription_plan_id"))
                    == PADDLE_MONTHLY_PRODUCT_ID
                ):
                    plan = PlanEnum.monthly
                else:
                    plan = PlanEnum.yearly

                sub.cancel_url = request.form.get("cancel_url")
                sub.update_url = request.form.get("update_url")
                sub.event_time = arrow.now()
                sub.next_bill_date = arrow.get(
                    request.form.get("next_bill_date"), "YYYY-MM-DD"
                ).date()
                sub.plan = plan

                # make sure to set the new plan as not-cancelled
                sub.cancelled = False

                db.session.commit()
            else:
                return "No such subscription", 400
        return "OK"


def init_extensions(app: Flask):
    login_manager.init_app(app)
    db.init_app(app)
    migrate.init_app(app)


def init_admin(app):
    admin = Admin(name="SimpleLogin", template_mode="bootstrap3")

    admin.init_app(app, index_view=SLAdminIndexView())
    admin.add_view(SLModelView(User, db.session))
    admin.add_view(SLModelView(Client, db.session))
    admin.add_view(SLModelView(Alias, db.session))
    admin.add_view(SLModelView(ClientUser, db.session))


def setup_do_not_track(app):
    @app.route("/dnt")
    def do_not_track():
        return """
        <script src="/static/local-storage-polyfill.js"></script>

        <script>
// Disable GoatCounter if this script is called

store.set('goatcounter-ignore', 't');

alert("GoatCounter disabled");

window.location.href = "/";

</script>
        """


if __name__ == "__main__":
    app = create_app()

    # enable flask toolbar
    # app.config["DEBUG_TB_PROFILER_ENABLED"] = True
    # app.config["DEBUG_TB_INTERCEPT_REDIRECTS"] = False
    #
    # toolbar = DebugToolbarExtension(app)

    # warning: only used in local
    if RESET_DB:
        LOG.warning("reset db, add fake data")
        with app.app_context():
            fake_data()

    if URL.startswith("https"):
        LOG.d("enable https")
        context = ssl.SSLContext(ssl.PROTOCOL_TLSv1_2)
        context.load_cert_chain("local_data/cert.pem", "local_data/key.pem")

        app.run(debug=True, port=7777, ssl_context=context)
    else:
        app.run(debug=True, port=7777)
