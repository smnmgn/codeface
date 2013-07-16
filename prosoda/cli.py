## This file is part of prosoda.  prosoda is free software: you can
## redistribute it and/or modify it under the terms of the GNU General Public
## License as published by the Free Software Foundation, version 2.
##
## This program is distributed in the hope that it will be useful, but WITHOUT
## ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
## FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
## details.
##
## You should have received a copy of the GNU General Public License
## along with this program; if not, write to the Free Software
## Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA
##
## Copyright 2013 by Siemens AG, Wolfgang Mauerer <wolfgang.mauerer@siemens.com>
## All Rights Reserved.
'''
Command-line interface driver for the prosoda package

Provides

'''
import argparse
import unittest
import os
from pkg_resources import resource_filename

from .logger import set_log_level, start_logfile, log
from .configuration import Configuration
from .dbmanager import DBManager
from .cluster.cluster import doProjectAnalysis
from .util import generate_report, layout_all_graphs, check4ctags
from .ts import dispatch_ts_analysis

def get_parser():
    parser = argparse.ArgumentParser(prog='prosoda',
                description='Program for Social Data Analysis')
    parser.add_argument('--loglevel', help='Choose the logging level',
                choices=['debug', 'info', 'warning', 'error'],
                default='info')
    parser.add_argument('--logfile', help='Save all debug logging into the'
                ' given log file')

    sub_parser = parser.add_subparsers(help='select action')
    test_parser = sub_parser.add_parser('test', help='Run tests')
    test_parser.set_defaults(func=cmd_test)
    test_parser.add_argument('-p', '--pattern', default="*",
                help='run only tests matching the given pattern')
    test_parser.add_argument('-u', '--unit', action='store_true',
                help='run only unit tests')

    run_parser = sub_parser.add_parser('run', help='Run analysis')
    run_parser.set_defaults(func=cmd_run)
    run_parser.add_argument('-c', '--config', help="Prosoda configuration file",
                default='prosoda.conf')
    run_parser.add_argument('-p', '--project', help="Project configuration file",
                required=True)
    run_parser.add_argument('resdir',
                        help="Directory to store analysis results in")
    run_parser.add_argument('gitdir',
                        help="Directory for git repositories")
    run_parser.add_argument('--no-report', action="store_true",
                        help="Skip LaTeX report generation (and dot compilation)")

    dyn_parser = sub_parser.add_parser('dynamic', help='Start R server for a dynamic graph')
    dyn_parser.set_defaults(func=cmd_dynamic)
    dyn_parser.add_argument('-c', '--config', help="Prosoda configuration file",
                default='prosoda.conf')
    dyn_parser.add_argument('graph', help="graph to show", default=None, nargs='?')
    dyn_parser.add_argument('-l', '--list', action="store_true", help="list available graphs")
    return parser


def cmd_run(args):
    '''Dispatch the ``run`` command.'''
    # First make all the args absolute
    resdir, gitdir = map(os.path.abspath, (args.resdir, args.gitdir))
    prosoda_conf, project_conf = map(os.path.abspath, (args.config, args.project))
    no_report = args.no_report
    del args

    conf = Configuration.load(prosoda_conf, project_conf)
    revs = conf["revisions"]
    rcs = conf["rcs"] # release candidate tags

    log.info("Processing project '{c[description]}'".format(c=conf))
    dbm = DBManager(conf)
    pid = dbm.getProjectID(conf["project"], conf["tagging"])

    # Fill table release_timeline with the release information
    # known so far (date is not yet available)
    for i in range(len(revs)):
        # We need to make sure that no entries are duplicated to avoid
        # creating malformed release databases
        dbm.doExec("SELECT * FROM release_timeline WHERE type='release' " +
                   "AND tag=%s AND projectId=%s", (revs[i], pid))

        if dbm.cur.rowcount < 1:
            dbm.doExecCommit("INSERT INTO release_timeline " +
                             "(type, tag, projectId) " +
                             "VALUES (%s, %s, %s)",
                             ("release", revs[i], pid))

    # Also construct the release ranges, again with the information known
    # so far
    for i in range(len(revs)-1):
        startID = dbm.getRevisionID(pid, revs[i])
        endID = dbm.getRevisionID(pid, revs[i+1])
        rcTag = rcs[i+1]
        rcID = None

        if (rcTag != None):
            dbm.doExec("SELECT * FROM release_timeline WHERE type='rc' " +
                   "AND tag=%s AND projectId=%s", (rcTag, pid))

            if dbm.cur.rowcount < 1:
                dbm.doExecCommit("INSERT INTO release_timeline " +
                                 "(type, tag, projectId) " +
                                 "VALUES (%s, %s, %s)",
                                 ("rc", rcTag, pid))
                rcID = dbm.getRCID(pid, rcTag)

        if (rcID != None):
            dbm.doExecCommit("INSERT INTO release_range " +
                             "(releaseStartId, releaseEndId, "+
                             "projectId, releaseRCStartId) " +
                             "VALUES (%s, %s, %s, %s)",
                             (startID, endID, pid, rcID))
        else:
            dbm.doExecCommit("INSERT INTO release_range " +
                             "(releaseStartId, releaseEndId, "+
                             "projectId) " +
                             "VALUES (%s, %s, %s)",
                             (startID, endID, pid))

    ## Obtain all release range ids created
    dbm.doExec("SELECT id FROM release_range WHERE projectId=%s", (pid))
    releaseRangeIds = dbm.doFetchAll()
    releaseRangeIds = [str(releaseRangeIds[i][0])
                       for i in range(0,len(releaseRangeIds))]

    #############################
    # Analyse all revision ranges
    for i in range(len(revs)-1):
        rev_resdir = os.path.join(resdir, conf["project"],
                              conf["tagging"],
                              "{0}-{1}".format(revs[i], revs[i+1]))

        # TODO: Sanity checks (ensure that git repo dir exists)
        if 'proximity' == conf["tagging"]:
            check4ctags()

        #######
        # STAGE 1: Commit analysis
        # TODO: Instead of calling an external python script, it
        # would maybe wiser to call a procedure...
        log.info("  -> Analysing commits {0}..{1}".format(revs[i], revs[i+1]))

        limit_history = True

        repo = os.path.join(gitdir, conf["repo"], ".git")
        doProjectAnalysis(conf, dbm, revs[i], revs[i+1], rcs[i+1], rev_resdir, repo, True, limit_history)

        #########
        # STAGE 2: Cluster analysis
        log.info("  -> Detecting clusters")
        cmd = []
        cmd.append(resource_filename(__name__, "R/cluster/persons.r"))
        cmd.extend(("-c", prosoda_conf))
        cmd.extend(("-p", project_conf))
        cmd.append(rev_resdir)
        cmd.append(releaseRangeIds[i])
        cwd = resource_filename(__name__, "R")
        old_cwd = os.getcwd()
        os.chdir(cwd)
        if os.system(" ".join(cmd)) != 0:
            log.critical("Error running R script '{}'".format(" ".join(cmd)))
            return 1
        os.chdir(old_cwd)

        #########
        # STAGE 3: Generate cluster graphs
        if not no_report:
            layout_all_graphs(rev_resdir)

        #########
        # STAGE 4: Report generation
        # Stage 4.1: Report preparation
        if not no_report:
            generate_report(revs[i], revs[i+1], rev_resdir)

    #########
    # Global stage 1: Time series generation
    log.info("=> Preparing time series data")
    dispatch_ts_analysis(resdir, dbm, conf)

    #########
    # Global stage 2: Time series analysis
    log.info("=> Analysing time series")
    cmd = []
    cmd.append(resource_filename(__name__, "R/analyse_ts.r"))
    cmd.extend(("-c", prosoda_conf))
    cmd.extend(("-p", project_conf))
    cmd.append(resdir)
    cwd = resource_filename(__name__, "R")
    old_cwd = os.getcwd()
    os.chdir(cwd)
    if os.system(" ".join(cmd)) != 0:
        log.critical("Error running R script '{}'".format(" ".join(cmd)))
        return 1
    os.chdir(old_cwd)
    log.info("prosoda run complete.")
    return 0

def cmd_dynamic(args):
    r_directory = resource_filename(__name__, "R")
    dyn_directory = resource_filename(__name__, "R/dynamic_graphs")
    if args.list or args.graph is None:
        if args.graph is None:
            log.critical("No dynamic graph given!")
        print('List of possible dynamic graphs:')
        for s in sorted(os.listdir(dyn_directory)):
            if s.endswith('.r'):
                print(" * " + s[:-len('.r')])
        return 1

    fn = os.path.join(dyn_directory, args.graph + ".r")
    cfg = os.path.abspath(args.config)
    if not os.path.exists(fn):
        log.critical('File "{}" not found!'.format(fn))
        return 1
    os.chdir(r_directory)
    cmd = "Rscript '{}' -c '{}'".format(fn, cfg)
    log.debug("Running command '{}'".format(cmd))
    os.system(cmd)

def cmd_test(args):
    '''Sub-command handler for the ``test`` command.'''
    unit_only=args.unit
    pattern=args.pattern
    del args
    test_path = os.path.join(os.path.dirname(__file__), 'test')
    print('\n===== running unittests =====\n')
    tests = unittest.TestLoader().discover(os.path.join(test_path, 'unit'),
        pattern='test_{}.py'.format(pattern), top_level_dir=test_path)
    unit_result = unittest.TextTestRunner(verbosity=1).run(tests)
    unit_success = not (unit_result.failures or unit_result.errors)
    if unit_only:
        if unit_success:
            print('\n===== unit tests succeeded :) =====')
        else:
            print('\n===== unit tests failed :( =====')
        return 0 if unit_success else 1
    print('\n===== running integration tests =====\n')
    tests = unittest.TestLoader().discover(os.path.join(test_path, 'integration'),
        pattern='test_{}.py'.format(pattern), top_level_dir=test_path)
    int_result = unittest.TextTestRunner(verbosity=2).run(tests)
    int_success = not (int_result.failures or int_result.errors)
    if unit_success and int_success:
            print('\n===== all tests succeeded :) =====')
    else:
            print('\n===== some tests failed :( =====')
    return 0 if unit_success and int_success else 1

def run(argv):
    parser = get_parser()
    # Note: The first argument of argv is the name of the command
    args = parser.parse_args(argv[1:])
    set_log_level(args.loglevel)
    if args.logfile:
        start_logfile(args.logfile, 'debug')
    return args.func(args)

def main():
    import sys
    return run(sys.argv)
