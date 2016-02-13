#!/usr/bin/env python2
# -*- coding: utf-8 -*-

import argparse
import io
import json
import logging
import os.path

from tornado.template import Template

from cms.db import SessionGen, Contest, ask_for_contest,\
    User, Task, Participation, Submission, Testcase, Evaluation
from cms.db.filecacher import FileCacher
from cms.grading import task_score
from cms.grading.scoretypes import get_score_type
from cms.grading.ScoreType import ScoreTypeGroup
from cms import SCORE_MODE_MAX


logger = logging.getLogger(__name__)


def tex_escape(s):
    from tornado.escape import to_unicode
    u_str = to_unicode(s)
    def char_escape(c):
        if c in "#$%&_{}":
            return "\\" + c
        elif c == "\\":
            return "{\\textbackslash}"
        elif c in "<>^~":
            return "\\char'\\" + c
        else:
            return c
    return u"".join(map(char_escape, u_str))

def shorten_eval_text(text):
    templates = [
        ["Output is correct", "AC"],
        ["Output isn't correct", "WA"],
        ["Execution timed out", "TLE"],
        ["Execution killed", "RE"],
        ["Execution failed", "RE"]
    ]
    for t in templates:
        if text.find(t[0]) >= 0:
            return t[1]
    logger.warning("Unknown eval text: %s", text)
    return text

# Based on function 'task_score' of cms/grading/__init__.py
def find_final_submission_result(participation, task):
    """Return the submission result which is used to determine the score"""

    submissions = [s for s in participation.submissions if s.task is task]
    submissions.sort(key=lambda s: s.timestamp)
    submissions.reverse() # reverse order

    if submissions == []:
        return None

    if task.score_mode == SCORE_MODE_MAX:
        targets = submissions
    else:
        targets = [submissions[0]] + [s for s in submissions[1:] if s.tokened()]

    final = None

    for s in targets:
        sr = s.get_result(task.active_dataset)
        if (sr is None) or (not sr.scored()):
            logger.error("Submission %d is not scored yet." % s.id)
        if sr.compilation_failed():
            continue
        if final is None or final.score < sr.score:
            final = sr

    return final

class SummaryFormatter:

    TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "summary.tex")

    def __init__(self, contest_id, export_target):

        self.contest_id = contest_id

        # If target is not provided, we use the contest's name.
        if export_target == "":
            with SessionGen() as session:
                contest = Contest.get_from_id(self.contest_id, session)
                export_target = "summary_%s.tex" % contest.name
                logger.warning("export_target not given, using \"%s\".",
                    export_target)
        self.export_target = export_target

        self.file_cacher = FileCacher()

    def do_export(self):
        """Run the actual export code."""

        if not os.path.exists(self.TEMPLATE_PATH):
            logger.critical("Tex template file"
                "not found (path: %s).", self.TEMPLATE_PATH)
            return False

        logger.info("Start exporting.")

        with SessionGen() as session:

            contest = Contest.get_from_id(self.contest_id, session)

            all_tasks = session.query(Task) \
                .filter(Task.contest == contest) \
                .order_by(Task.num).all()

            all_testcases = [session.query(Testcase) \
                .filter(Testcase.dataset == task.active_dataset) \
                .order_by(Testcase.codename).all()
                for task in all_tasks
            ]

            contest_info = { "description": contest.description }
            users_info = []

            for participation in session.query(Participation) \
                .filter(Participation.contest == contest) \
                .order_by(Participation.user_id).all():

                user = participation.user

                u_info = {
                    "username": user.username,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "tasks": []
                }

                for task, testcases in zip(all_tasks, all_testcases):

                    scoretype = get_score_type(dataset=task.active_dataset)
                    assert(issubclass(scoretype.__class__, ScoreTypeGroup))
                    score_type_parameters = json.loads(
                        task.active_dataset.score_type_parameters)
                    targets = scoretype.retrieve_target_testcases()

                    max_score = scoretype.max_scores()[0]
                    sr = find_final_submission_result(participation, task)

                    t_info = {
                        "num": task.num,
                        "title": task.title,
                        "max_score": round(max_score, task.score_precision),
                        "testcases": [],
                        "subtasks": []
                    }

                    if sr is None:
                        t_info["score"] = 0.0
                    else:
                        t_info["score"] = round(sr.score, task.score_precision)

                    for tc in testcases:

                        tc_data = {
                            "name": tc.codename
                        }

                        if sr is None:
                            tc_data["outcome"] = 0.0
                            tc_data["text"] = "N/A"
                            tc_data["execution_time"] = 0.0
                            tc_data["execution_memory"] = 0.0
                        else:
                            ev = sr.get_evaluation(tc)
                            tc_data["outcome"] = ev.outcome
                            tc_data["text"] = shorten_eval_text(ev.text)
                            tc_data["execution_time"] = ev.execution_time
                            tc_data["execution_memory"] = ev.execution_memory

                        t_info["testcases"] += [tc_data]

                    if sr is None:
                        scores = [0.0] * len(score_type_parameters)
                    else:
                        scores = [s["score"]
                            for s in json.loads(scoretype.compute_score(sr)[1])]

                    for st_idx, subtask in enumerate(score_type_parameters):

                        tcs = subtask[1]\
                            .replace("\\A", "")\
                            .replace("\\Z", "")\
                            .replace("(", "")\
                            .replace(")", "")\
                            .replace(".*", "*")\
                            .split("|")

                        st_info = {
                            "name": "Subtask %d" % st_idx,
                            "score": scores[st_idx],
                            "max_score": float(subtask[0]),
                            "testcases": tcs
                        }

                        t_info["subtasks"] += [st_info]

                    u_info["tasks"] += [t_info]

                score = sum(t["score"] for t in u_info["tasks"])
                max_score = sum(t["max_score"] for t in u_info["tasks"])
                u_info["score"] = round(score, contest.score_precision)
                u_info["max_score"] = round(max_score, contest.score_precision)

                users_info += [u_info]

        with io.open(self.TEMPLATE_PATH, "r", encoding="utf-8") as template_fp, \
            io.open(self.export_target, "w", encoding="utf-8") as tex_fp:

            template = template_fp.read()
            data = {
                "contest": contest_info,
                "users": users_info,
                "tex_escape": tex_escape
            }
            tex_string = Template(template, autoescape="tex_escape").generate(**data)
            tex_fp.write(unicode(tex_string, encoding="utf-8"))

        logger.info("Export finished.")

        return True

def main():
    """Parse arguments and launch process."""
    parser = argparse.ArgumentParser(
        description="Outputs summary for each contestants.")
    parser.add_argument("-c", "--contest-id", action="store", type=int,
                        help="id of contest to export")
    parser.add_argument("export_target", nargs='?', default="",
                        help="target file for export")

    args = parser.parse_args()

    if args.contest_id is None:
        args.contest_id = ask_for_contest()

    SummaryFormatter(contest_id=args.contest_id,
                     export_target=args.export_target).do_export()


if __name__ == "__main__":
    main()
