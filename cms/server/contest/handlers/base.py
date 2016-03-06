#!/usr/bin/env python2
# -*- coding: utf-8 -*-

# Contest Management System - http://cms-dev.github.io/
# Copyright © 2010-2014 Giovanni Mascellani <mascellani@poisson.phc.unipi.it>
# Copyright © 2010-2016 Stefano Maggiolo <s.maggiolo@gmail.com>
# Copyright © 2010-2012 Matteo Boscariol <boscarim@hotmail.com>
# Copyright © 2012-2015 Luca Wehrstedt <luca.wehrstedt@gmail.com>
# Copyright © 2013 Bernard Blackham <bernard@largestprime.net>
# Copyright © 2014 Artem Iglikov <artem.iglikov@gmail.com>
# Copyright © 2014 Fabian Gundlach <320pointsguy@gmail.com>
# Copyright © 2015 William Di Luigi <williamdiluigi@gmail.com>
# Copyright © 2016 Myungwoo Chun <mc.tamaki@gmail.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Base handler classes for CWS.

"""

from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals

import logging
import pickle
import socket
import struct
import traceback

from datetime import timedelta

import tornado.web

from werkzeug.datastructures import LanguageAccept
from werkzeug.http import parse_accept_header

from cms import config
from cms.db import Contest, Participation, User
from cms.server import CommonRequestHandler, compute_actual_phase, \
    file_handler_gen, get_url_root
from cms.locale import filter_language_codes
from cmscommon.datetime import get_timezone, make_datetime, make_timestamp
from cmscommon.isocodes import translate_language_code, \
    translate_language_country_code


logger = logging.getLogger(__name__)


NOTIFICATION_ERROR = "error"
NOTIFICATION_WARNING = "warning"
NOTIFICATION_SUCCESS = "success"


def check_ip(client, wanted):
    """Return if client IP belongs to the wanted subnet.

    client (string): IP address to verify.
    wanted (string): IP address or subnet to check against.

    return (bool): whether client equals wanted (if the latter is an IP
        address) or client belongs to wanted (if it's a subnet).

    """
    wanted, sep, subnet = wanted.partition('/')
    subnet = 32 if sep == "" else int(subnet)
    snmask = 2 ** 32 - 2 ** (32 - subnet)
    wanted = struct.unpack(">I", socket.inet_aton(wanted))[0]
    client = struct.unpack(">I", socket.inet_aton(client))[0]
    return (wanted & snmask) == (client & snmask)


class BaseHandler(CommonRequestHandler):
    """Base RequestHandler for this application.

    All the RequestHandler classes in this application should be a
    child of this class.

    """

    def __init__(self, *args, **kwargs):
        super(BaseHandler, self).__init__(*args, **kwargs)
        self.cookie_lang = None
        self.browser_lang = None
        self.langs = None
        self._ = None

    def prepare(self):
        """This method is executed at the beginning of each request.

        """
        super(BaseHandler, self).prepare()
        self.contest = Contest.get_from_id(self.application.service.contest,
                                           self.sql_session)

        self._ = self.locale.translate

        self.r_params = self.render_params()

    def get_current_user(self):
        """The name is get_current_user because tornado wants it that
        way, but this is really a get_current_participation.

        Gets the current participation from cookies.

        If a valid cookie is retrieved, return a Participation tuple
        (specifically: the Participation involving the username
        specified in the cookie and the current contest).

        Otherwise (e.g. the user exists but doesn't participate in the
        current contest), return None.

        """
        remote_ip = self.request.remote_ip
        if self.contest.ip_autologin:
            self.clear_cookie("login")
            participations = self.sql_session.query(Participation)\
                .filter(Participation.contest == self.contest)\
                .filter(Participation.ip == remote_ip)\
                .all()
            if len(participations) == 1:
                return participations[0]

            if len(participations) > 1:
                logger.error("Multiple users have IP %s." % (remote_ip))
            else:
                logger.error("No user has IP %s" % (remote_ip))

            # If IP autologin is set, we do not allow password logins.
            return None

        if self.get_secure_cookie("login") is None:
            return None

        # Parse cookie.
        try:
            cookie = pickle.loads(self.get_secure_cookie("login"))
            username = cookie[0]
            password = cookie[1]
            last_update = make_datetime(cookie[2])
        except:
            self.clear_cookie("login")
            return None

        # Check if the cookie is expired.
        if self.timestamp - last_update > \
                timedelta(seconds=config.cookie_duration):
            self.clear_cookie("login")
            return None

        # Load user from DB.
        user = self.sql_session.query(User)\
            .filter(User.username == username)\
            .first()

        # Check if user exists.
        if user is None:
            self.clear_cookie("login")
            return None

        # Load participation from DB.
        participation = self.sql_session.query(Participation)\
            .filter(Participation.contest == self.contest)\
            .filter(Participation.user == user)\
            .first()

        # Check if participaton exists.
        if participation is None:
            self.clear_cookie("login")
            return None

        # If a contest-specific password is defined, use that. If it's
        # not, use the user's main password.
        if participation.password is None:
            correct_password = user.password
        else:
            correct_password = participation.password

        # Check if user is allowed to login.
        if password != correct_password:
            self.clear_cookie("login")
            return None

        # Check if user is using the right IP (or is on the right subnet)
        if self.contest.ip_restriction and participation.ip is not None \
                and not check_ip(self.request.remote_ip, participation.ip):
            self.clear_cookie("login")
            return None

        # Check if user is hidden
        if participation.hidden and self.contest.block_hidden_participations:
            self.clear_cookie("login")
            return None

        if self.refresh_cookie:
            self.set_secure_cookie("login",
                                   pickle.dumps((user.username,
                                                 password,
                                                 make_timestamp())),
                                   expires_days=None)

        return participation

    def get_user_locale(self):
        self.langs = self.application.service.langs
        lang_codes = self.langs.keys()

        if len(self.contest.allowed_localizations) > 0:
            lang_codes = filter_language_codes(
                lang_codes, self.contest.allowed_localizations)

        # Select the one the user likes most.
        basic_lang = lang_codes[0].replace("_", "-") \
            if len(self.contest.allowed_localizations) else 'en'
        http_langs = [lang_code.replace("_", "-") for lang_code in lang_codes]
        self.browser_lang = parse_accept_header(
            self.request.headers.get("Accept-Language", ""),
            LanguageAccept).best_match(http_langs, basic_lang)

        self.cookie_lang = self.get_cookie("language", None)

        if self.cookie_lang in http_langs:
            lang_code = self.cookie_lang
        else:
            lang_code = self.browser_lang

        self.set_header("Content-Language", lang_code)
        return self.langs[lang_code.replace("-", "_")]

    @staticmethod
    def _get_token_status(obj):
        """Return the status of the tokens for the given object.

        obj (Contest or Task): an object that has the token_* attributes.
        return (int): one of 0 (disabled), 1 (enabled/finite) and 2
                      (enabled/infinite).

        """
        if obj.token_mode == "disabled":
            return 0
        elif obj.token_mode == "finite":
            return 1
        elif obj.token_mode == "infinite":
            return 2
        else:
            raise RuntimeError("Unknown token_mode value.")

    def render_params(self):
        """Return the default render params used by almost all handlers.

        return (dict): default render params

        """
        ret = {}
        ret["timestamp"] = self.timestamp
        ret["contest"] = self.contest
        ret["url_root"] = get_url_root(self.request.path)

        ret["phase"] = self.contest.phase(self.timestamp)

        ret["printing_enabled"] = (config.printer is not None)
        ret["questions_enabled"] = self.contest.allow_questions
        ret["testing_enabled"] = self.contest.allow_user_tests

        ret["registration_phase"] = False
        ret["registration_enabled"] = config.registration_enabled

        if self.current_user is not None:
            participation = self.current_user

            res = compute_actual_phase(
                self.timestamp, self.contest.start, self.contest.stop,
                self.contest.per_user_time, participation.starting_time,
                participation.delay_time, participation.extra_time)

            ret["actual_phase"], ret["current_phase_begin"], \
                ret["current_phase_end"], ret["valid_phase_begin"], \
                ret["valid_phase_end"] = res

            if ret["actual_phase"] == 0:
                ret["phase"] = 0

            # set the timezone used to format timestamps
            ret["timezone"] = get_timezone(participation.user, self.contest)

        # some information about token configuration
        ret["tokens_contest"] = self._get_token_status(self.contest)

        t_tokens = sum(self._get_token_status(t) for t in self.contest.tasks)
        if t_tokens == 0:
            ret["tokens_tasks"] = 0  # all disabled
        elif t_tokens == 2 * len(self.contest.tasks):
            ret["tokens_tasks"] = 2  # all infinite
        else:
            ret["tokens_tasks"] = 1  # all finite or mixed

        # TODO Now all language names are shown in the active language.
        # It would be better to show them in the corresponding one.
        ret["lang_names"] = {}

        # Get language codes for allowed localizations
        lang_codes = self.langs.keys()
        if len(self.contest.allowed_localizations) > 0:
            lang_codes = filter_language_codes(
                lang_codes, self.contest.allowed_localizations)
        for lang_code, trans in self.langs.iteritems():
            language_name = None
            # Filter lang_codes with allowed localizations
            if lang_code not in lang_codes:
                continue
            try:
                language_name = translate_language_country_code(
                    lang_code, trans)
            except ValueError:
                language_name = translate_language_code(
                    lang_code, trans)
            ret["lang_names"][lang_code.replace("_", "-")] = language_name

        ret["cookie_lang"] = self.cookie_lang
        ret["browser_lang"] = self.browser_lang

        return ret

    def finish(self, *args, **kwds):
        """Finish this response, ending the HTTP request.

        We override this method in order to properly close the database.

        TODO - Now that we have greenlet support, this method could be
        refactored in terms of context manager or something like
        that. So far I'm leaving it to minimize changes.

        """
        if hasattr(self, "sql_session"):
            try:
                self.sql_session.close()
            except Exception as error:
                logger.warning("Couldn't close SQL connection: %r", error)
        try:
            tornado.web.RequestHandler.finish(self, *args, **kwds)
        except IOError:
            # When the client closes the connection before we reply,
            # Tornado raises an IOError exception, that would pollute
            # our log with unnecessarily critical messages
            logger.debug("Connection closed before our reply.")

    def write_error(self, status_code, **kwargs):
        if "exc_info" in kwargs and \
                kwargs["exc_info"][0] != tornado.web.HTTPError:
            exc_info = kwargs["exc_info"]
            logger.error(
                "Uncaught exception (%r) while processing a request: %s",
                exc_info[1], ''.join(traceback.format_exception(*exc_info)))

        # We assume that if r_params is defined then we have at least
        # the data we need to display a basic template with the error
        # information. If r_params is not defined (i.e. something went
        # *really* bad) we simply return a basic textual error notice.
        if hasattr(self, 'r_params'):
            self.render("error.html", status_code=status_code, **self.r_params)
        else:
            self.write("A critical error has occurred :-(")
            self.finish()


class StaticFileGzHandler(tornado.web.StaticFileHandler):
    """Handle files which may be gzip-compressed on the filesystem."""
    def validate_absolute_path(self, root, absolute_path):
        self.is_gzipped = False
        try:
            return tornado.web.StaticFileHandler.validate_absolute_path(
                self, root, absolute_path)
        except tornado.web.HTTPError as e:
            if e.status_code != 404:
                raise
            self.is_gzipped = True
            self.absolute_path = \
                tornado.web.StaticFileHandler.validate_absolute_path(
                    self, root, absolute_path + ".gz")
            self.set_header("Content-encoding", "gzip")
            return self.absolute_path

    def get_content_type(self):
        if self.is_gzipped:
            return "text/plain"
        else:
            return tornado.web.StaticFileHandler.get_content_type(self)


FileHandler = file_handler_gen(BaseHandler)
