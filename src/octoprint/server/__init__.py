# coding=utf-8
from __future__ import absolute_import

__author__ = "Gina Häußge <osd@foosel.net>"
__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'
__copyright__ = "Copyright (C) 2014 The OctoPrint Project - Released under terms of the AGPLv3 License"

import uuid
from sockjs.tornado import SockJSRouter
from flask import Flask, render_template, send_from_directory, g, request, make_response, session
from flask.ext.login import LoginManager
from flask.ext.principal import Principal, Permission, RoleNeed, identity_loaded, UserNeed
from flask.ext.babel import Babel, gettext, ngettext
from babel import Locale
from watchdog.observers import Observer
from collections import defaultdict
from urlparse import urlparse
from jinja2 import Template

import os
import logging
import logging.config
import atexit

SUCCESS = {}
NO_CONTENT = ("", 204)

app = Flask("octoprint")
babel = Babel(app)
debug = False

printer = None
printerProfileManager = None
fileManager = None
slicingManager = None
analysisQueue = None
userManager = None
eventManager = None
loginManager = None
pluginManager = None
appSessionManager = None

principals = Principal(app)
admin_permission = Permission(RoleNeed("admin"))
user_permission = Permission(RoleNeed("user"))

# only import the octoprint stuff down here, as it might depend on things defined above to be initialized already
from octoprint.printer import Printer, getConnectionOptions
from octoprint.printer.profile import PrinterProfileManager
from octoprint.settings import settings
import octoprint.users as users
import octoprint.events as events
import octoprint.plugin
import octoprint.timelapse
import octoprint._version
import octoprint.util
import octoprint.filemanager.storage
import octoprint.filemanager.analysis
import octoprint.slicing

from . import util


UI_API_KEY = ''.join('%02X' % ord(z) for z in uuid.uuid4().bytes)

versions = octoprint._version.get_versions()
VERSION = versions['version']
BRANCH = versions['branch'] if 'branch' in versions else None
DISPLAY_VERSION = "%s (%s branch)" % (VERSION, BRANCH) if BRANCH else VERSION
del versions


def get_available_locale_identifiers(locales):
	result = set()

	# add available translations
	for locale in locales:
		result.add(locale.language)
		if locale.territory:
			# if a territory is specified, add that too
			result.add("%s_%s" % (locale.language, locale.territory))

	return result


LOCALES = [Locale.parse("en")] + babel.list_translations()
LANGUAGES = get_available_locale_identifiers(LOCALES)


@app.before_request
def before_request():
	g.locale = get_locale()


@babel.localeselector
def get_locale():
	if "l10n" in request.values:
		return Locale.negotiate([request.values["l10n"]], LANGUAGES)
	return request.accept_languages.best_match(LANGUAGES)


@app.route("/")
def index():



	#~~ extract data from asset plugins

	asset_plugins = pluginManager.get_implementations(octoprint.plugin.AssetPlugin)
	asset_plugin_urls = dict()
	for name, implementation in asset_plugins.items():
		asset_plugin_urls[name] = implementation.get_assets()

	#~~ extract data from template plugins

	template_plugins = pluginManager.get_implementations(octoprint.plugin.TemplatePlugin)

	# rules for transforming template configs to template entries
	rules = dict(
		navbar=dict(div=lambda x: "navbar_plugin_" + x, template=lambda x: x + "_navbar.jinja2", to_entry=lambda data: data),
		sidebar=dict(div=lambda x: "sidebar_plugin_" + x, template=lambda x: x + "_sidebar.jinja2", to_entry=lambda data: (data["name"], data)),
		tab=dict(div=lambda x: "tab_plugin_" + x, template=lambda x: x + "_tab.jinja2", to_entry=lambda data: (data["name"], data)),
		settings=dict(div=lambda x: "settings_plugin_" + x, template=lambda x: x + "_settings.jinja2", to_entry=lambda data: (data["name"], data)),
		generic=dict(template=lambda x: x + ".jinja2", to_entry=lambda data: data)
	)

	plugin_vars = dict()
	plugin_includes_navbar = []
	plugin_includes_sidebar = []
	plugin_includes_tabs = []
	plugin_includes_settings = []
	plugin_includes_generic = []
	plugin_names = template_plugins.keys()
	for name, implementation in template_plugins.items():
		vars = implementation.get_template_vars()
		if not isinstance(vars, dict):
			vars = dict()

		for var_name, var_value in vars.items():
			plugin_vars["plugin_" + name + "_" + var_name] = var_value

		configs = implementation.get_template_configs()
		if not isinstance(configs, (list, tuple)):
			configs = []

		includes = _process_template_configs(name, implementation, configs, rules)

		plugin_includes_navbar += includes["navbar"]
		plugin_includes_sidebar += includes["sidebar"]
		plugin_includes_tabs += includes["tab"]
		plugin_includes_settings += includes["settings"]
		plugin_includes_generic += includes["generic"]

	#~~ navbar

	navbar_entries = plugin_includes_navbar + [
		dict(template="navbar/settings.jinja2", _div="navbar_settings", styles=["display: none"], data_bind="visible: loginState.isAdmin", custom_bindings=False),
		dict(template="navbar/systemmenu.jinja2", _div="navbar_systemmenu", styles=["display: none"], classes=["dropdown"], data_bind="visible: loginState.isAdmin", custom_bindings=False),
		dict(template="navbar/login.jinja2", _div="navbar_login", classes=["dropdown"], custom_bindings=False)
	]

	#~~ sidebar

	sidebar_entries = [
		(gettext("Connection"), dict(template="sidebar/connection.jinja2", _div="connection", icon="signal", styles_wrapper=["display: none"], data_bind="visible: loginState.isAdmin")),
		(gettext("State"), dict(template="sidebar/state.jinja2", _div="state", icon="info-sign")),
		(gettext("Files"), dict(template="sidebar/files.jinja2", _div="files", icon="list", classes_content=["overflow_visible"], header_addon="sidebar/files_header.jinja2"))
	] + plugin_includes_sidebar

	#~~ tabs

	tab_entries = [
		(gettext("Temperature"), dict(template="tabs/temperature.jinja2", _div="temp")),
		(gettext("Control"), dict(template="tabs/control.jinja2", _div="control")),
		(gettext("GCode Viewer"), dict(template="tabs/gcodeviewer.jinja2", _div="gcode")),
		(gettext("Terminal"), dict(template="tabs/terminal.jinja2", _div="term")),
		(gettext("Timelapse"), dict(template="tabs/timelapse.jinja2", _div="timelapse"))
	] + plugin_includes_tabs

	#~~ settings dialog

	settings_entries = [
		(gettext("Printer"), None),
		(gettext("Serial Connection"), dict(template="dialogs/settings/serialconnection.jinja2", _div="settings_serialConnection", custom_bindings=False)),
		(gettext("Printer Profiles"), dict(template="dialogs/settings/printerprofiles.jinja2", _div="settings_printerProfiles", custom_bindings=False)),
		(gettext("Temperatures"), dict(template="dialogs/settings/temperatures.jinja2", _div="settings_temperature", custom_bindings=False)),
		(gettext("Terminal Filters"), dict(template="dialogs/settings/terminalfilters.jinja2", _div="settings_terminalFilters", custom_bindings=False)),
		(gettext("Features"), None),
		(gettext("Features"), dict(template="dialogs/settings/features.jinja2", _div="settings_features", custom_bindings=False)),
		(gettext("Webcam"), dict(template="dialogs/settings/webcam.jinja2", _div="settings_webcam", custom_bindings=False)),
		(gettext("Access Control"), dict(template="dialogs/settings/accesscontrol.jinja2", _div="settings_users", custom_bindings=False)),
		(gettext("API"), dict(template="dialogs/settings/api.jinja2", _div="settings_api", custom_bindings=False)),
		(gettext("OctoPrint"), None),
		(gettext("Folders"), dict(template="dialogs/settings/folders.jinja2", _div="settings_folders", custom_bindings=False)),
		(gettext("Appearance"), dict(template="dialogs/settings/appearance.jinja2", _div="settings_appearance", custom_bindings=False)),
		(gettext("Logs"), dict(template="dialogs/settings/logs.jinja2", _div="settings_logs"))
	]
	if len(plugin_includes_settings):
		settings_entries.append((gettext("Plugins"), None))
		settings_entries.extend(sorted(plugin_includes_settings, key=lambda x: x[0]))

	#~~ combine webcam address

	settingsWebcamStream = settings().get(["webcam", "stream"])
	if settingsWebcamStream:
		access_address = urlparse(request.url).hostname
		webcamStream = Template(settingsWebcamStream).render(access_address=access_address)
	else:
		webcamStream = settingsWebcamStream

	#~~ prepare full set of template vars for rendering

	render_kwargs = dict(
		webcamStream=webcamStream,
		enableTimelapse=(settings().get(["webcam", "snapshot"]) is not None and settings().get(["webcam", "ffmpeg"]) is not None),
		enableGCodeVisualizer=settings().get(["gcodeViewer", "enabled"]),
		enableTemperatureGraph=settings().get(["feature", "temperatureGraph"]),
		enableSystemMenu=settings().get(["system"]) is not None and settings().get(["system", "actions"]) is not None and len(settings().get(["system", "actions"])) > 0,
		enableAccessControl=userManager is not None,
		enableSdSupport=settings().get(["feature", "sdSupport"]),
		firstRun=settings().getBoolean(["server", "firstRun"]) and (userManager is None or not userManager.hasBeenCustomized()),
		debug=debug,
		version=VERSION,
		display_version=DISPLAY_VERSION,
		stylesheet=settings().get(["devel", "stylesheet"]),
		gcodeMobileThreshold=settings().get(["gcodeViewer", "mobileSizeThreshold"]),
		gcodeThreshold=settings().get(["gcodeViewer", "sizeThreshold"]),
		uiApiKey=UI_API_KEY,
		navbarEntries=navbar_entries,
		sidebarEntries=sidebar_entries,
		tabEntries=tab_entries,
		settingsEntries=settings_entries,
		genericEntries=plugin_includes_generic,
		pluginNames=plugin_names,
		assetPlugins=asset_plugin_urls,
	)
	render_kwargs.update(plugin_vars)

	#~~ render!

	return render_template(
		"index.jinja2",
		**render_kwargs
	)


def _process_template_configs(name, implementation, configs, rules):
	from jinja2.exceptions import TemplateNotFound

	counters = dict(
		navbar=1,
		sidebar=1,
		tab=1,
		settings=1
	)
	includes = defaultdict(list)

	for config in configs:
		if not isinstance(config, dict):
			continue
		if not "type" in config:
			continue

		template_type = config["type"]
		del config["type"]

		if not template_type in rules:
			continue
		rule = rules[template_type]

		data = _process_template_config(name, implementation, rule, config=config, counter=counters[template_type])
		if data is None:
			continue

		includes[template_type].append(rule["to_entry"](data))
		counters[template_type] += 1

	for template_type in rules:
		if len(includes[template_type]) == 0:
			# if no template of that type was added by the config, we'll try to use the default template name
			rule = rules[template_type]
			data = _process_template_config(name, implementation, rule)
			if data is not None:
				try:
					app.jinja_env.get_or_select_template(data["template"])
				except TemplateNotFound:
					pass
				else:
					includes[template_type].append(rule["to_entry"](data))

	return includes

def _process_template_config(name, implementation, rule, config=None, counter=1):
	if "mandatory" in rule:
		for mandatory in rule["mandatory"]:
			if not mandatory in config:
				return None

	if config is None:
		config = dict()
	data = dict(config)

	if "div" in rule:
		data["_div"] = rule["div"](name)
		if "suffix" in data:
			data["_div"] += "_" + data["suffix"]
			del data["suffix"]
		elif counter > 1:
			data["_div"] += "_%d" % counter
	if not "template" in data:
		data["template"] = rule["template"](name)
	if not "name" in data:
		data["name"] = implementation._plugin_name
	if not "custom_bindings" in data or data["custom_bindings"]:
		data_bind = "allowBindings: true"
		if "data_bind" in data:
			data_bind = data_bind + ", " + data["data_bind"]
		data["data_bind"] = data_bind

	return data

@app.route("/robots.txt")
def robotsTxt():
	return send_from_directory(app.static_folder, "robots.txt")


@app.route("/plugin_assets/<string:name>/<path:filename>")
def plugin_assets(name, filename):
	asset_plugins = pluginManager.get_implementations(octoprint.plugin.AssetPlugin)

	if not name in asset_plugins:
		return make_response("Asset not found", 404)
	asset_plugin = asset_plugins[name]
	asset_folder = asset_plugin.get_asset_folder()
	if asset_folder is None:
		return make_response("Asset not found", 404)

	return send_from_directory(asset_folder, filename)


@identity_loaded.connect_via(app)
def on_identity_loaded(sender, identity):
	user = load_user(identity.id)
	if user is None:
		return

	identity.provides.add(UserNeed(user.get_name()))
	if user.is_user():
		identity.provides.add(RoleNeed("user"))
	if user.is_admin():
		identity.provides.add(RoleNeed("admin"))


def load_user(id):
	if id == "_api":
		return users.ApiUser()

	if session and "usersession.id" in session:
		sessionid = session["usersession.id"]
	else:
		sessionid = None

	if userManager is not None:
		if sessionid:
			return userManager.findUser(username=id, session=sessionid)
		else:
			return userManager.findUser(username=id)
	return users.DummyUser()


#~~ startup code


class Server():
	def __init__(self, configfile=None, basedir=None, host="0.0.0.0", port=5000, debug=False, allowRoot=False, logConf=None):
		self._configfile = configfile
		self._basedir = basedir
		self._host = host
		self._port = port
		self._debug = debug
		self._allowRoot = allowRoot
		self._logConf = logConf
		self._server = None

	def run(self):
		if not self._allowRoot:
			self._checkForRoot()

		global printer
		global printerProfileManager
		global fileManager
		global slicingManager
		global analysisQueue
		global userManager
		global eventManager
		global loginManager
		global pluginManager
		global appSessionManager
		global debug

		from tornado.ioloop import IOLoop
		from tornado.web import Application

		import sys

		debug = self._debug

		# first initialize the settings singleton and make sure it uses given configfile and basedir if available
		self._initSettings(self._configfile, self._basedir)

		# then initialize logging
		self._initLogging(self._debug, self._logConf)
		logger = logging.getLogger(__name__)
		def exception_logger(exc_type, exc_value, exc_tb):
			logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))
		sys.excepthook = exception_logger
		logger.info("Starting OctoPrint %s" % DISPLAY_VERSION)

		# then initialize the plugin manager
		pluginManager = octoprint.plugin.plugin_manager(init=True)

		printerProfileManager = PrinterProfileManager()
		eventManager = events.eventManager()
		analysisQueue = octoprint.filemanager.analysis.AnalysisQueue()
		slicingManager = octoprint.slicing.SlicingManager(settings().getBaseFolder("slicingProfiles"), printerProfileManager)
		storage_managers = dict()
		storage_managers[octoprint.filemanager.FileDestinations.LOCAL] = octoprint.filemanager.storage.LocalFileStorage(settings().getBaseFolder("uploads"))
		fileManager = octoprint.filemanager.FileManager(analysisQueue, slicingManager, printerProfileManager, initial_storage_managers=storage_managers)
		printer = Printer(fileManager, analysisQueue, printerProfileManager)
		appSessionManager = util.flask.AppSessionManager()

		pluginManager.initialize_implementations(dict(
		    plugin_manager=pluginManager,
		    printer_profile_manager=printerProfileManager,
		    event_bus=eventManager,
		    analysis_queue=analysisQueue,
		    slicing_manager=slicingManager,
		    file_manager=fileManager,
		    printer=printer,
		    app_session_manager=appSessionManager,
		))

		# configure additional template folders for jinja2
		template_plugins = pluginManager.get_implementations(octoprint.plugin.TemplatePlugin)
		additional_template_folders = []
		for plugin in template_plugins.values():
			folder = plugin.get_template_folder()
			if folder is not None:
				additional_template_folders.append(plugin.get_template_folder())

		import jinja2
		jinja_loader = jinja2.ChoiceLoader([
			app.jinja_loader,
			jinja2.FileSystemLoader(additional_template_folders)
		])
		app.jinja_loader = jinja_loader
		del jinja2

		# configure timelapse
		octoprint.timelapse.configureTimelapse()

		# setup command triggers
		events.CommandTrigger(printer)
		if self._debug:
			events.DebugEventListener()

		if settings().getBoolean(["accessControl", "enabled"]):
			userManagerName = settings().get(["accessControl", "userManager"])
			try:
				clazz = octoprint.util.getClass(userManagerName)
				userManager = clazz()
			except AttributeError, e:
				logger.exception("Could not instantiate user manager %s, will run with accessControl disabled!" % userManagerName)

		app.wsgi_app = util.ReverseProxied(
			app.wsgi_app,
			settings().get(["server", "reverseProxy", "prefixHeader"]),
			settings().get(["server", "reverseProxy", "schemeHeader"]),
			settings().get(["server", "reverseProxy", "hostHeader"]),
			settings().get(["server", "reverseProxy", "prefixFallback"]),
			settings().get(["server", "reverseProxy", "schemeFallback"]),
			settings().get(["server", "reverseProxy", "hostFallback"])
		)

		secret_key = settings().get(["server", "secretKey"])
		if not secret_key:
			import string
			from random import choice
			chars = string.ascii_lowercase + string.ascii_uppercase + string.digits
			secret_key = "".join(choice(chars) for _ in xrange(32))
			settings().set(["server", "secretKey"], secret_key)
			settings().save()
		app.secret_key = secret_key
		loginManager = LoginManager()
		loginManager.session_protection = "strong"
		loginManager.user_callback = load_user
		if userManager is None:
			loginManager.anonymous_user = users.DummyUser
			principals.identity_loaders.appendleft(users.dummy_identity_loader)
		loginManager.init_app(app)

		if self._host is None:
			self._host = settings().get(["server", "host"])
		if self._port is None:
			self._port = settings().getInt(["server", "port"])

		app.debug = self._debug

		from octoprint.server.api import api
		from octoprint.server.apps import apps

		# register API blueprint
		app.register_blueprint(api, url_prefix="/api")
		app.register_blueprint(apps, url_prefix="/apps")

		# also register any blueprints defined in BlueprintPlugins
		blueprint_plugins = octoprint.plugin.plugin_manager().get_implementations(octoprint.plugin.BlueprintPlugin)
		for name, plugin in blueprint_plugins.items():
			blueprint = plugin.get_blueprint()
			if blueprint is None:
				continue

			if plugin.is_blueprint_protected():
				from octoprint.server.util import apiKeyRequestHandler, corsResponseHandler
				blueprint.before_request(apiKeyRequestHandler)
				blueprint.after_request(corsResponseHandler)

			url_prefix = "/plugin/{name}".format(name=name)
			app.register_blueprint(blueprint, url_prefix=url_prefix)
			logger.debug("Registered API of plugin {name} under URL prefix {url_prefix}".format(name=name, url_prefix=url_prefix))

		self._router = SockJSRouter(self._createSocketConnection, "/sockjs")

		upload_suffixes = dict(name=settings().get(["server", "uploads", "nameSuffix"]), path=settings().get(["server", "uploads", "pathSuffix"]))
		self._tornado_app = Application(self._router.urls + [
			(r"/downloads/timelapse/([^/]*\.mpg)", util.tornado.LargeResponseHandler, dict(path=settings().getBaseFolder("timelapse"), as_attachment=True)),
			(r"/downloads/files/local/([^/]*\.(gco|gcode|g))", util.tornado.LargeResponseHandler, dict(path=settings().getBaseFolder("uploads"), as_attachment=True)),
			(r"/downloads/logs/([^/]*)", util.tornado.LargeResponseHandler, dict(path=settings().getBaseFolder("logs"), as_attachment=True, access_validation=util.tornado.access_validation_factory(app, loginManager, util.flask.admin_validator))),
			(r"/downloads/camera/current", util.tornado.UrlForwardHandler, dict(url=settings().get(["webcam", "snapshot"]), as_attachment=True, access_validation=util.tornado.access_validation_factory(app, loginManager, util.flask.user_validator))),
			(r".*", util.tornado.UploadStorageFallbackHandler, dict(fallback=util.tornado.WsgiInputContainer(app.wsgi_app), file_prefix="octoprint-file-upload-", file_suffix=".tmp", suffixes=upload_suffixes))
		])
		max_body_sizes = [
			("POST", r"/api/files/([^/]*)", settings().getInt(["server", "uploads", "maxSize"]))
		]
		self._server = util.tornado.CustomHTTPServer(self._tornado_app, max_body_sizes=max_body_sizes, default_max_body_size=settings().getInt(["server", "maxSize"]))
		self._server.listen(self._port, address=self._host)

		eventManager.fire(events.Events.STARTUP)
		if settings().getBoolean(["serial", "autoconnect"]):
			(port, baudrate) = settings().get(["serial", "port"]), settings().getInt(["serial", "baudrate"])
			printer_profile = printerProfileManager.get_default()
			connectionOptions = getConnectionOptions()
			if port in connectionOptions["ports"]:
				printer.connect(port=port, baudrate=baudrate, profile=printer_profile["id"] if "id" in printer_profile else "_default")

		# start up watchdogs
		observer = Observer()
		observer.schedule(util.watchdog.GcodeWatchdogHandler(fileManager, printer), settings().getBaseFolder("watched"))
		observer.start()

		ioloop = IOLoop.instance()

		# run our startup plugins
		octoprint.plugin.call_plugin(octoprint.plugin.StartupPlugin,
		                             "on_startup",
		                             args=(self._host, self._port))

		# prepare our after startup function
		def on_after_startup():
			logger.info("Listening on http://%s:%d" % (self._host, self._port))

			# now this is somewhat ugly, but the issue is the following: startup plugins might want to do things for
			# which they need the server to be already alive (e.g. for being able to resolve urls, such as favicons
			# or service xmls or the like). While they are working though the ioloop would block. Therefore we'll
			# create a single use thread in which to perform our after-startup-tasks, start that and hand back
			# control to the ioloop
			def work():
				octoprint.plugin.call_plugin(octoprint.plugin.StartupPlugin,
				                             "on_after_startup")
			import threading
			threading.Thread(target=work).start()
		ioloop.add_callback(on_after_startup)

		# prepare our shutdown function
		def on_shutdown():
			logger.info("Goodbye!")
			observer.stop()
			observer.join()
			octoprint.plugin.call_plugin(octoprint.plugin.ShutdownPlugin,
			                             "on_shutdown")
		atexit.register(on_shutdown)

		try:
			ioloop.start()
		except KeyboardInterrupt:
			pass
		except:
			logger.fatal("Now that is embarrassing... Something really really went wrong here. Please report this including the stacktrace below in OctoPrint's bugtracker. Thanks!")
			logger.exception("Stacktrace follows:")

	def _createSocketConnection(self, session):
		global printer, fileManager, analysisQueue, userManager, eventManager
		return util.sockjs.PrinterStateConnection(printer, fileManager, analysisQueue, userManager, eventManager, pluginManager, session)

	def _checkForRoot(self):
		if "geteuid" in dir(os) and os.geteuid() == 0:
			exit("You should not run OctoPrint as root!")

	def _initSettings(self, configfile, basedir):
		settings(init=True, basedir=basedir, configfile=configfile)

	def _initLogging(self, debug, logConf=None):
		defaultConfig = {
			"version": 1,
			"formatters": {
				"simple": {
					"format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
				}
			},
			"handlers": {
				"console": {
					"class": "logging.StreamHandler",
					"level": "DEBUG",
					"formatter": "simple",
					"stream": "ext://sys.stdout"
				},
				"file": {
					"class": "logging.handlers.TimedRotatingFileHandler",
					"level": "DEBUG",
					"formatter": "simple",
					"when": "D",
					"backupCount": "1",
					"filename": os.path.join(settings().getBaseFolder("logs"), "octoprint.log")
				},
				"serialFile": {
					"class": "logging.handlers.RotatingFileHandler",
					"level": "DEBUG",
					"formatter": "simple",
					"maxBytes": 2 * 1024 * 1024, # let's limit the serial log to 2MB in size
					"filename": os.path.join(settings().getBaseFolder("logs"), "serial.log")
				}
			},
			"loggers": {
				"SERIAL": {
					"level": "CRITICAL",
					"handlers": ["serialFile"],
					"propagate": False
				},
				"tornado.application": {
					"level": "ERROR"
				}
			},
			"root": {
				"level": "INFO",
				"handlers": ["console", "file"]
			}
		}

		if debug:
			defaultConfig["root"]["level"] = "DEBUG"

		if logConf is None:
			logConf = os.path.join(settings().settings_dir, "logging.yaml")

		configFromFile = {}
		if os.path.exists(logConf) and os.path.isfile(logConf):
			import yaml
			with open(logConf, "r") as f:
				configFromFile = yaml.safe_load(f)

		config = octoprint.util.dict_merge(defaultConfig, configFromFile)
		logging.config.dictConfig(config)

		if settings().getBoolean(["serial", "log"]):
			# enable debug logging to serial.log
			logging.getLogger("SERIAL").setLevel(logging.DEBUG)
			logging.getLogger("SERIAL").debug("Enabling serial logging")

if __name__ == "__main__":
	server = Server()
	server.run()
