import subprocess
import string

from flask import Flask, request
from flask_bcrypt import Bcrypt
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy

# Flask convention is to use `app`
app = Flask(__name__)  # pylint: disable=invalid-name
app.bcrypt = Bcrypt()
app.db = SQLAlchemy()
app.login_manager = LoginManager()


def init_app():
    app.bcrypt.init_app(app)
    app.db.init_app(app)
    app.login_manager.init_app(app)

try:
    from collegejump._version import __version__
except ImportError:
    print("WARN: no _version.py; is the package installed? using SCM instead")
    import collegejump.scmtools
    __version__ = collegejump.scmtools.get_scm_version()

# Make the version easily accessible
app.config['VERSION'] = __version__

# Set SQLAlchemy options that'll never change
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# We import the views here so that the application still mostly works even if
# the main() function isn't ever run.
import collegejump.views # pylint: disable=cyclic-import,wrong-import-position
