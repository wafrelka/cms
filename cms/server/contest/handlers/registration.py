#!/usr/bin/env python2
# -*- coding: utf-8 -*-

"""Registration-related handlers for CWS.

"""

from __future__ import absolute_import
from __future__ import print_function
from __future__ import unicode_literals

import logging
import tornado.web

from cms.db import Contest, Participation, User
from cms.server import filter_ascii

from .base import BaseHandler, NOTIFICATION_ERROR, NOTIFICATION_SUCCESS


logger = logging.getLogger(__name__)


class RegisterHandler(BaseHandler):
    """Register handler.

    """
    def get(self):
        if not self.r_params["registration_enabled"]:
            raise tornado.web.HTTPError(404)

        self.r_params["registration_phase"] = True
        self.r_params["registration_user"] = None
        self.render("base.html", **self.r_params)

    def post(self):
        if not self.r_params["registration_enabled"]:
            raise tornado.web.HTTPError(404)

        first_name = self.get_argument("first_name", "")
        last_name = self.get_argument("last_name", "")
        username = self.get_argument("username", "")

        if username != filter_ascii(username) or username == "":
            logger.warning("Registration error: Cannot use that user name")
            self.redirect("/register?register_error=mal")
            return

        user = self.sql_session.query(User) \
            .filter(User.username == username) \
            .first()

        if user is not None:
            logger.warning("Registration error: The user already exists")
            self.redirect("/register?register_error=dup")
            return

        import random
        chars = "23456789abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"
        password = "".join([random.choice(chars) for _ in xrange(8)])

        user = User(first_name, last_name, username, password=password)
        self.sql_session.add(user)

        self.sql_session.commit()

        self.r_params["registration_phase"] = True
        self.r_params["registration_user"] = user

        # to avoid consistency error in RWS
        self.application.service.proxy_service.reinitialize()

        self.render("base.html", **self.r_params)

class EditInfoHandler(BaseHandler):
    """Provides form for editing user information.

    """
    @tornado.web.authenticated
    def get(self):
        if not self.r_params["registration_enabled"]:
            raise tornado.web.HTTPError(404)
        self.render("edit_info.html", **self.r_params)

    @tornado.web.authenticated
    def post(self):
        if not self.r_params["registration_enabled"]:
            raise tornado.web.HTTPError(404)

        self.current_user.user.first_name = self.get_argument("first_name", "")
        self.current_user.user.last_name = self.get_argument("last_name", "")
        # self.sql_session.add(question)
        self.sql_session.commit()

        logger.info("User %s changed their information."
                    % self.current_user.user.username)

        # Add "All ok" notification.
        self.application.service.add_notification(
            self.current_user.user.username,
            self.timestamp,
            self._("Updated user information"),
            self._("Your information has been changed."),
            NOTIFICATION_SUCCESS)

        self.application.service.proxy_service.reinitialize()
        self.redirect("/")
