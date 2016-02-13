#!/usr/bin/env python2
# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import unicode_literals
from __future__ import print_function

import io
import logging
import os
import os.path
import sys
import tempfile
import zipfile
import yaml
import json
import re
import StringIO
from dateutil import parser, tz
from datetime import timedelta

from cms import LANGUAGES, LANGUAGE_TO_HEADER_EXT_MAP, \
    SCORE_MODE_MAX
from cms.db import Contest, User, Task, Statement, Attachment, \
    SubmissionFormatElement, Dataset, Manager, Testcase
from cmscontrib import touch

from .base_loader import ContestLoader, TaskLoader, UserLoader

logger = logging.getLogger(__name__)


# Patch PyYAML to make it load all strings as unicode instead of str
# (see http://stackoverflow.com/questions/2890146).
def construct_yaml_str(self, node):
    return self.construct_scalar(node)
yaml.Loader.add_constructor('tag:yaml.org,2002:str', construct_yaml_str)
yaml.SafeLoader.add_constructor('tag:yaml.org,2002:str', construct_yaml_str)

def make_timedelta(t):
    return timedelta(seconds=t)

def make_datetime(s):
    return parser.parse(s).astimezone(tz.tzutc()).replace(tzinfo=None)

def load_yaml(path):
    return yaml.safe_load(io.open(path, 'rt', encoding='utf-8'))

def same_path(a, b):
    return os.path.normpath(a) == os.path.normpath(b)

def get_mtime(fname):
    return os.stat(fname).st_mtime

def convert_glob_to_regexp(g):
    SPECIAL_CHARS = '\\.^$+{}[]|()'
    for c in SPECIAL_CHARS:
        g = g.replace(c, '\\' + c)
    g = g.replace('*', '.*')
    g = g.replace('?', '.')
    return g
def convert_globlist_to_regexp(gs):
    rs = ['(' + convert_glob_to_regexp(g) + ')' for g in gs]
    return '\A' + '|'.join(rs) + '\Z'

def try_assign(dest, src, keyname, conv=lambda i:i):
    if keyname in src:
        dest[keyname] = conv(src[keyname])
def assign(dest, src, keyname, conv=lambda i:i):
    dest[keyname] = conv(src[keyname])


# FIXME: split the class
#        (before splitting, we must rebuild the structure around loaders)
class ImprovedImoJudgeFormatLoader(ContestLoader, TaskLoader, UserLoader):

    """Load a contest, task or user stored using Improved ImoJudge-like format.

    Given the filesystem location of a contest, task or user, stored
    using Improved ImoJudge-like format, parse those files and directories
    to produce data that can be consumed by CMS.
    This format is INCOMPATIBLE with former ImoJudge-like format (used in 2015).

    As a ContestLoader,
    the path must be the directory that contains \"contest-iif.yaml\" file.

    As a TaskLoader, the path must be the directory that contains
    \"etc\" sub directory which contains \"task-iif.yaml\" file.
    Also, the specified directory must be a direct child of
    the contest directory (i.e. a directory that contains \"contest-iif.yaml\").

    """

    short_name = 'improved_imoj'
    description = 'Improved ImoJudge-like format'

    @staticmethod
    def detect(path):
        """See docstring in class Loader."""
        return os.path.exists(os.path.join(path, 'contest-iif.yaml')) or \
            os.path.exists(os.path.join(path, 'etc', 'task-iif.yaml')) or \
            os.path.exists(os.path.normpath(os.path.join(path, '..',
            'contest-iif.yaml')))

    def get_task_loader(self, taskname):
        """See docstring in class Loader."""

        conf_path = os.path.join(self.path, 'contest-iif.yaml')

        if not os.path.exists(conf_path):
            logger.critical("File missing: \"contest-iif.yaml\"")
            return None

        conf = load_yaml(conf_path)
        targets = [t for t in conf['tasks'] if t['name'] == taskname]

        if len(targets) == 0:
            logger.critical("The specified task cannot be found.")
            return None
        if len(targets) > 1:
            logger.critical("There are multiple tasks with the same task name.")
            return None

        taskdir = os.path.join(self.path, targets[0]['dir'])
        task_conf_path = os.path.join(taskdir, 'etc', 'task-iif.yaml')

        if not os.path.exists(task_conf_path):
            logger.critical("File missing: \"task-iif.yaml\"")
            return None

        # TODO: check whether taskdir is a direct child of the contest dir

        return self.__class__(taskdir, self.file_cacher)

    def get_contest(self):
        """See docstring in class ContestLoader."""

        conf_path = os.path.join(self.path, 'contest-iif.yaml')

        if not os.path.exists(conf_path):
            logger.critical("File missing: \"contest-iif.yaml\"")
            return None

        conf = load_yaml(conf_path)
        name = conf['name']

        logger.info("Loading parameters for contest \"%s\".", name)

        # Here we update the time of the last import
        touch(os.path.join(self.path, '.itime_contest'))
        # If this file is not deleted, then the import failed
        touch(os.path.join(self.path, '.import_error_contest'))

        dirname = os.path.dirname(self.path)
        if name != dirname:
            logger.warning(
                "The directory name and the contest name is different.")

        args = {}

        assign(args, conf, 'name')
        assign(args, conf, 'description')
        assign(args, conf, 'languages')
        try_assign(args, conf, 'start', make_datetime)
        try_assign(args, conf, 'stop', make_datetime)

        try_assign(args, conf, 'score_precision')
        try_assign(args, conf, 'max_submission_number')
        try_assign(args, conf, 'max_user_test_number')
        try_assign(args, conf, 'min_submission_interval', make_timedelta)
        try_assign(args, conf, 'min_user_test_interval', make_timedelta)

        assign(args, conf, 'token_mode')
        try_assign(args, conf, 'token_max_number')
        try_assign(args, conf, 'token_min_interval', make_timedelta)
        try_assign(args, conf, 'token_gen_initial')
        try_assign(args, conf, 'token_gen_number')
        try_assign(args, conf, 'token_gen_interval', make_timedelta)
        try_assign(args, conf, 'token_gen_max')

        if 'timezone' not in conf:
            conf['timezone'] = 'Asia/Tokyo'
        assign(args, conf, 'timezone')

        tasks = [t['name'] for t in conf['tasks']]
        participations = conf['users']

        if any(l not in LANGUAGES for l in args['languages']):
            logger.critical("Language \"%s\" is not supported.", l)
            return None

        # Import was successful
        os.remove(os.path.join(self.path, '.import_error_contest'))

        logger.info("Contest parameters loaded.")

        return Contest(**args), tasks, participations

    def get_user(self):
        """See docstring in class UserLoader."""

        conf_path = os.path.join(os.path.dirname(self.path), 'contest-iif.yaml')

        if not os.path.exists(conf_path):
            logger.critical("File missing: \"contest-iif.yaml\"")
            return None

        conf = load_yaml(conf_path)
        # due to the terrible AddUser script
        username = os.path.basename(self.path)

        logger.info("Loading parameters for user %s.", username)

        targets = [u for u in conf['users'] if u['username'] == username]

        if len(targets) == 0:
            logger.critical("The specified user cannot be found.")
            return None
        if len(targets) > 1:
            logger.critical("There are multiple users with the same user name.")
            return None

        args = {}
        user_conf = targets[0]

        if 'first_name' not in user_conf:
            user_conf['first_name'] = ""
        if 'last_name' not in user_conf:
            user_conf['last_name'] = user_conf['username']

        assign(args, user_conf, 'username')
        assign(args, user_conf, 'password')
        assign(args, user_conf, 'first_name')
        assign(args, user_conf, 'last_name')
        try_assign(args, user_conf, 'hidden')

        logger.info("User parameters loaded.")

        return User(**args)

    def get_task(self, get_statement=True):
        """See docstring in class TaskLoader."""

        contest_path = os.path.join(self.path, '..')
        conf_path = os.path.join(self.path, 'etc', 'task-iif.yaml')
        contest_conf_path = os.path.join(contest_path, 'contest-iif.yaml')

        if not os.path.exists(conf_path):
            logger.critical("File missing: \"task-iif.yaml\"")
            return None
        if not os.path.exists(contest_conf_path):
            logger.critical("File missing: \"contest-iif.yaml\"")
            return None

        conf = load_yaml(conf_path)
        contest_conf = load_yaml(contest_conf_path)

        contest_tasks = [t for t in contest_conf['tasks']
            if same_path(os.path.join(contest_path, t['dir']), self.path)]

        if len(contest_tasks) == 0:
            logger.critical("The specified task cannot be found "
                "in the contest setting file.")
            return None
        if len(contest_tasks) > 1:
            logger.critical("There are multiple tasks with "
                "the same directory setting.")
            return None

        name = contest_tasks[0]['name']
        allowed_lang = contest_conf['languages']

        logger.info("Loading parameters for task %s.", name)

        # Here we update the time of the last import
        touch(os.path.join(self.path, '.itime_task'))
        # If this file is not deleted, then the import failed
        touch(os.path.join(self.path, '.import_error_task'))

        task_args = {}

        task_args['name'] = name
        assign(task_args, conf, 'title')

        if task_args['name'] == task_args['title']:
            logger.warning("Short name and title are same. Please check.")

        try_assign(task_args, conf, 'score_precision')
        try_assign(task_args, conf, 'max_submission_number')
        try_assign(task_args, conf, 'max_user_test_number')
        try_assign(task_args, conf, 'min_submission_interval', make_timedelta)
        try_assign(task_args, conf, 'min_user_test_interval', make_timedelta)

        if 'token_mode' not in conf:
            conf['token_mode'] = 'disabled'

        assign(task_args, conf, 'token_mode')
        try_assign(task_args, conf, 'token_max_number')
        try_assign(task_args, conf, 'token_min_interval', make_timedelta)
        try_assign(task_args, conf, 'token_gen_initial')
        try_assign(task_args, conf, 'token_gen_number')
        try_assign(task_args, conf, 'token_gen_interval', make_timedelta)
        try_assign(task_args, conf, 'token_gen_max')

        if 'score_mode' not in conf:
            conf['score_mode'] = SCORE_MODE_MAX
        assign(task_args, conf, 'score_mode')

        # Statements
        if get_statement:

            primary_lang = conf.get('primary_language', 'ja')
            pdf_dir = os.path.join(self.path, 'task')
            pdf_paths = [
                (os.path.join(pdf_dir, name + ".pdf"), primary_lang),
                (os.path.join(pdf_dir, name + "-ja.pdf"), 'ja'),
                (os.path.join(pdf_dir, name + "-en.pdf"), 'en')]

            task_args['statements'] = []
            for path, lang in pdf_paths:
                if os.path.exists(path):
                    digest = self.file_cacher.put_file_from_path(path,
                        "Statement for task %s (lang: %s)" % (name, lang))
                    task_args['statements'] += [Statement(lang, digest)]

            if len(task_args['statements']) == 0:
                logger.critical("Couldn't find any task statement.")
                return None

            task_args['primary_statements'] = '["%s"]' % primary_lang

        # Attachments
        task_args['attachments'] = []
        dist_path = os.path.join(self.path, 'dist')

        if os.path.exists(dist_path):

            zfn = tempfile.mkstemp('iif-loader-', '.zip')
            with zipfile.ZipFile(zfn[1], 'w', zipfile.ZIP_STORED) as zf:
                for fname in os.listdir(dist_path):
                    if not fname.endswith('.zip'):
                        zf.write(os.path.join(dist_path, fname),
                            os.path.join(name, fname))
            zip_digest = self.file_cacher.put_file_from_path(
                zfn[1], "Distribution archive for task %s" % name)
            task_args['attachments'] += [Attachment(name + '.zip', zip_digest)]
            os.remove(zfn[1])

            for fname in os.listdir(dist_path):
                if fname.endswith('.zip'):
                    digest = self.file_cacher.put_file_from_path(
                        os.path.join(dist_path, fname),
                        "Distribution file for task %s" % name)
                    task_args['attachments'] += [Attachment(fname, digest)]

        # maybe modified in the succeeding process
        task_args['submission_format'] = [
            SubmissionFormatElement("%s.%%l" % name)]

        task = Task(**task_args)

        ds_args = {}

        ds_args['task'] = task
        ds_args['description'] = conf.get('version', 'default-version')
        ds_args['autojudge'] = False

        testcases = []
        feedback = {}
        input_digests = {}

        feedback_globs = conf.get('feedback', ['*'])
        feedback_regexp = convert_globlist_to_regexp(feedback_globs)
        feedback_re = re.compile(feedback_regexp)

        # Testcases enumeration
        for f in os.listdir(os.path.join(self.path, 'in')):
            m = re.match(r'\A(.*)\.txt\Z', f)
            if m:
                tc = m.group(1)
                testcases.append(tc)
                feedback[tc] = feedback_re.match(tc) is not None
            else:
                logger.warning("File \"%s\" was not added to testcases" % f)
        testcases.sort()

        ds_args['testcases'] = []

        for tc in testcases:

            in_path = os.path.join(self.path, 'in', tc + '.txt')
            out_path = os.path.join(self.path, 'out', tc + '.txt')

            input_digest = self.file_cacher.put_file_from_path(
                in_path, "Input %s for task %s" % (tc, name))
            output_digest = None

            if os.path.exists(out_path):
                output_digest = self.file_cacher.put_file_from_path(
                    out_path, "Output %s for task %s" % (tc, name))
            else:
                logger.warning("Output file for %s cannot be found." % tc)
                dummy = StringIO.StringIO('')
                output_digest = self.file_cacher.put_file_from_fobj(
                    dummy, "Dummy output %s for task %s" % (tc, name))
                dummy.close()

            ds_args['testcases'] += [Testcase(tc, feedback[tc],
                input_digest, output_digest)]
            input_digests[tc] = input_digest

        # Score type specific processing
        scoretype = conf.get('score_type', 'normal')

        # FIXME: support Kanji-like score type
        if scoretype == 'normal':

            score_params = [
                [st['point'], convert_globlist_to_regexp(st['targets'])]
                for st in conf['subtasks']]
            ds_args['score_type_parameters'] = json.dumps(score_params)
            ds_args['score_type'] = 'GroupMin'

        else:
            logger.critical("Score type \"%s\" is "
                "currently unsupported.", scoretype)
            return None

        cms_path = os.path.join(self.path, 'cms')

        compilation_param = 'alone'
        infile_param = ''
        outfile_param = ''
        eval_param = 'diff'

        stub_found = False
        manager_found = False

        ds_args["managers"] = []

        # Auto generation for manager/checker
        compilation_pairs = [
            ['manager.cpp', 'manager'],
            ['checker.cpp', 'checker']]
        for src_name, dst_name in compilation_pairs:
            src = os.path.join(cms_path, src_name)
            dst = os.path.join(cms_path, dst_name)
            if os.path.exists(src):
                has_src_changed = True
                if os.path.exists(dst):
                    has_src_changed = get_mtime(src) > get_mtime(dst)
                if has_src_changed:
                    logger.info("Auto-generation for %s." % dst)
                    os.system("g++ -std=c++11 -O2 -Wall -static %s -o %s"
                              % (src, dst))

        # Additional headers
        if os.path.exists(cms_path):
            for fname in os.listdir(cms_path):

                if any(fname.endswith(h) for h in
                    LANGUAGE_TO_HEADER_EXT_MAP.itervalues()):

                    digest = self.file_cacher.put_file_from_path(
                        os.path.join(cms_path, fname),
                        "Header \"%s\" for task %s" % (fname, name))
                    ds_args['managers'] += [Manager(fname, digest)]

        # Graders
        if any(os.path.exists(os.path.join(cms_path, 'grader.%s' % l))
            for l in allowed_lang):

            compilation_param = 'grader'

            for l in allowed_lang:

                grader_path = os.path.join(cms_path, 'grader.%s' % l)
                if os.path.exists(grader_path):
                    digest = self.file_cacher.put_file_from_path(
                        grader_path,
                        "Grader for task %s (language: %s)" % (name, l))
                    ds_args['managers'] += [Manager('grader.%s' % l, digest)]
                else:
                    logger.warning("Grader for language %s not found.", l)

        # Stubs
        if any(os.path.exists(os.path.join(cms_path, 'stub.%s' % l))
            for l in allowed_lang):

            stub_found = True

            for l in allowed_lang:

                stub_path = os.path.join(cms_path, 'stub.%s' % l)
                if os.path.exists(stub_path):
                    digest = self.file_cacher.put_file_from_path(
                        stub_path,
                        "Stub for task %s (language: %s)" % (name, l))
                    ds_args['managers'] += [Manager('stub.%s' % l, digest)]
                else:
                    logger.warning("Stub for language %s not found.", l)

        # Manager
        if os.path.exists(os.path.join(cms_path, 'manager')):

            manager_found = True
            digest = self.file_cacher.put_file_from_path(
                os.path.join(cms_path, 'manager'),
                "Manager for task %s" % name)
            ds_args['managers'] += [Manager('manager', digest)]

        # Checker
        if os.path.exists(os.path.join(cms_path, 'checker')):
            eval_param = 'comparator'
            digest = self.file_cacher.put_file_from_path(
                os.path.join(cms_path, 'checker'),
                "Checker for task %s" % name)
            ds_args['managers'] += [Manager('checker', digest)]

        # Task type specific processing
        tasktype = conf.get('task_type', 'Batch')

        if tasktype == 'Batch':

            assign(ds_args, conf, 'time_limit', conv=float)
            assign(ds_args, conf, 'memory_limit')

            ds_args['task_type'] = 'Batch'
            ds_args['task_type_parameters'] = \
                '["%s", ["%s", "%s"], "%s"]' % \
                (compilation_param, infile_param, outfile_param, eval_param)

        elif tasktype == 'OutputOnly':

            task.submission_format = [
                SubmissionFormatElement('output_%s.txt' % tc)
                for tc in sorted(testcases)]
            task.attachments += [
                Attachment('input_%s.txt' % tc, input_digests[tc])
                for tc in sorted(testcases)]

            ds_args['task_type'] = 'OutputOnly'
            ds_args['task_type_parameters'] = '["%s"]' % eval_param

        elif tasktype == 'Communication':

            if not stub_found:
                logger.critical("Stub is required for Communication task.")
                return None
            if not manager_found:
                logger.critical("Manager is required for Communication task.")
                return None

            assign(ds_args, conf, 'time_limit', conv=float)
            assign(ds_args, conf, 'memory_limit')

            ds_args['task_type'] = 'Communication'
            ds_args['task_type_parameters'] = '[]'

        else:
            logger.critical("Task type \"%s\" is "
                "currently unsupported.", tasktype)
            return None

        dataset = Dataset(**ds_args)
        task.active_dataset = dataset

        # Here we update the time of the last import
        # (because of autogeneration of manager/checker,
        # we should update the time after the import)
        touch(os.path.join(self.path, '.itime_task'))

        # Import was successful
        os.remove(os.path.join(self.path, ".import_error_task"))

        logger.info("Task parameters loaded.")

        return task

    def contest_has_changed(self):
        """See docstring in class ContestLoader."""

        conf_path = os.path.join(self.path, 'contest-iif.yaml')

        if not os.path.exists(conf_path):
            logger.critical("File missing: \"contest-iif.yaml\"")
            sys.exit(1)

        # If there is no .itime file, we assume that the contest has changed
        if not os.path.exists(os.path.join(self.path, '.itime_contest')):
            return True

        itime = get_mtime(os.path.join(self.path, '.itime_contest'))

        # Check if contest.yaml has changed
        if get_mtime(conf_path) > itime:
            return True

        if os.path.exists(os.path.join(self.path, '.import_error_contest')):
            logger.warning("Last attempt to import contest %s failed, I'm not "
                           "trying again. After fixing the error, delete the "
                           "file .import_error_contest", name)
            sys.exit(1) # XXX: is this a correct behavior ???

        return False

    def user_has_changed(self):
        """See docstring in class UserLoader."""
        # This works as users are kept inside contest-iif.yaml, so changing
        # them alters the last modified time of contest-iif.yaml.
        return self.contest_has_changed()

    def task_has_changed(self):
        """See docstring in class TaskLoader."""

        conf_path = os.path.join(self.path, 'etc', 'task-iif.yaml')

        if not os.path.exists(conf_path):
            logger.critical("File missing: \"task.yaml\"")
            sys.exit(1) # XXX: is this a correct behavior ???

        # If there is no .itime file, we assume that the task has changed
        if not os.path.exists(os.path.join(self.path, '.itime_task')):
            return True

        itime = get_mtime(os.path.join(self.path, '.itime_task'))

        # Enumerate related files
        files = []

        target_dirs = ['dist', 'in', 'out', 'task', 'cms']

        for d in target_dirs:
            p = os.path.join(self.path, d)
            if os.path.exists(p):
                for fname in os.listdir(p):
                    files.append(os.path.join(p, fname))

        files.append(os.path.join(self.path, 'etc', 'task-iif.yaml'))

        # Check is any of the files have changed
        for path in files:
            if os.path.exists(path):
                if get_mtime(path) > itime:
                    return True

        # FIXME: cannot detect some modifications
        #        (example: swapping directories between some tasks)

        if os.path.exists(os.path.join(self.path, ".import_error_task")):
            logger.warning("Last attempt to import task %s failed, I'm not "
                           "trying again. After fixing the error, delete the "
                           "file .import_error", name)
            sys.exit(1) # XXX: is this a correct behavior ???

        return False
