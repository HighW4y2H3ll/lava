#!/usr/bin/python

import argparse
import atexit
import json
import lockfile
import os
import psycopg2
import random
import re
import shlex
import shutil
import signal
import signal
import string
import subprocess32
import sys
import time

from os.path import basename, dirname, join, abspath

from lava import *


start_time = time.time()


project = None
# this is how much code we add to top of any file with main fn in it
NUM_LINES_MAIN_INSTR = 5
debugging = True



# run lavatool on this file to inject any parts of this list of bugs
# offset will be nonzero if file contains main and therefore
# has already been instrumented with a bunch of defs of lava_get and lava_set and so on
def inject_bugs_into_src(bugs, filename, offset):
    global query_build
    global bugs_build
    global lavatool
    global lavadb
    buglist = ','.join([str(bug.id) for bug in bugs])
    cmd = lava_tool + ' -action=inject -bug-list=\"' + buglist \
        + '\" -lava-db=' + lavadb + ' -p ' + bugs_build \
        + ' -main_instr_correction=' + (str(offset)) \
        + ' ' + filename \
        + ' ' + '-project-file=' + project_file
    return run_cmd_nto(cmd, None, None)


# run lavatool on this file and add defns for lava_get and lava_set
def instrument_main(filename):
    global query_build
    global bugs_build
    global lavatool
    global lavadb
    filename_bug_part = bugs_build + "/" + filename
    cmd = lava_tool + ' -action=main -bug-list=\"\"' \
        + ' -lava-db=' + lavadb + ' -p ' + bugs_build \
        + ' ' + filename_bug_part \
        + ' ' + '-project-file=' + project_file
    run_cmd_nto(cmd, None, None)


def get_suffix(fn):
    split = basename(fn).split(".")
    if len(split) == 1:
        return ""
    else:
        return "." + split[-1]


# here's how to run the built program
def run_prog(install_dir, input_file, timeout):
    cmd = project['command'].format(install_dir=install_dir,input_file=input_file)
    print cmd
    envv = {}
    lib_path = project['library_path'].format(install_dir=install_dir)
    envv["LD_LIBRARY_PATH"] = join(install_dir, lib_path)
    return run_cmd(cmd, install_dir, envv, timeout) # shell=True)


def printable(text):
    return ''.join([ '.' if c not in string.printable else c for c in text])

def add_run_row(build_id, fuzz, exitcode, lines, success):
    lines = lines.translate(None, '\'\"')
    lines = printable(lines[0:1024])
    conn = get_conn(project)
    cur = conn.cursor()
    # NB: ignoring binpath for now
    sql = "INSERT into run (build_id, fuzz, exitcode, output_lines, success) VALUES (" +\
        (str(build_id)) + "," + (str(fuzz)) + "," + (str(exitcode)) + ",\'" + lines + "\'," + (str(success)) + ");"
    print sql
    cur.execute(sql)
    # need to do all three of these in order for the writes to db to actually happen
    cur.close()
    conn.commit()
    conn.close()



if __name__ == "__main__":

    update_db = False
    parser = argparse.ArgumentParser(description='Inject and test LAVA bugs.')
    parser.add_argument('project', type=argparse.FileType('r'),
            help = 'JSON project file')
    parser.add_argument('-b', '--bugid', action="store", default=-1,
            help = 'Bug id (otherwise, highest scored will be chosen)')
    parser.add_argument('-r', '--randomize', action='store_true', default = False,
            help = 'Choose the next bug randomly rather than by score')
    parser.add_argument('-m', '--many', action="store", default=-1,
            help = 'Inject this many bugs (chosen randomly)')
    parser.add_argument('-l', '--buglist', action="store", default=False,
            help = 'Inject this list of bugs')


    args = parser.parse_args()
    project = json.load(args.project)
    project_file = args.project.name

    # Set up our globals now that we have a project
    db = LavaDatabase(project)

    timeout = project['timeout']

    # This is top-level directory for our LAVA stuff.
    top_dir = join(project['directory'], project['name'])
    lava_dir = dirname(dirname(abspath(sys.argv[0])))
    lava_tool = join(lava_dir, 'src_clang', 'build', 'lavaTool')

    # This should be {{directory}}/{{name}}/bugs
    bugs_top_dir = join(top_dir, 'bugs')

    try:
        os.makedirs(bugs_top_dir)
    except: pass

    # This is where we're going to do our injection. We need to make sure it's
    # not being used by another inject.py.
    bugs_parent = ""
    candidate = 0
    bugs_lock = None
    while bugs_parent == "":
        candidate_path = join(bugs_top_dir, str(candidate))
        lock = lockfile.LockFile(candidate_path)
        try:
            lock.acquire(timeout=-1)
            bugs_parent = join(candidate_path)
            bugs_lock = lock
        except lockfile.AlreadyLocked:
            candidate += 1

    print "Using dir", bugs_parent

    atexit.register(bugs_lock.release)
    for sig in [signal.SIGINT, signal.SIGTERM]:
        signal.signal(sig, lambda s, f: sys.exit(0))

    try:
        os.mkdir(bugs_parent)
    except: pass

    if 'source_root' in project:
        source_root = project['source_root']
    else:
        tar_files = subprocess32.check_output(['tar', 'tf', project['tarfile']], stderr=sys.stderr)
        source_root = tar_files.splitlines()[0].split(os.path.sep)[0]

    queries_build = join(top_dir, source_root)
    bugs_build = join(bugs_parent, source_root)
    bugs_install = join(bugs_build, 'lava-install')
    # Make sure directories and btrace is ready for bug injection.
    def run(args, **kwargs):
        print "run(",
        print args,
        print ")"
        subprocess32.check_call(args, cwd=bugs_build,
                stdout=sys.stdout, stderr=sys.stderr, **kwargs)
    if not os.path.exists(bugs_build):
        subprocess32.check_call(['tar', 'xf', project['tarfile'],
            '-C', bugs_parent], stderr=sys.stderr)
    if not os.path.exists(join(bugs_build, '.git')):
        run(['git', 'init'])
        run(['git', 'add', '-A', '.'])
        run(['git', 'commit', '-m', 'Unmodified source.'])
    if not os.path.exists(join(bugs_build, 'btrace.log')):
        run(shlex.split(project['configure']) + ['--prefix=' + bugs_install])
        run([join(lava_dir, 'btrace', 'sw-btrace')] + shlex.split(project['make']))

    lavadb = join(top_dir, 'lavadb')

    main_files = set(project['main_file'])

    if not os.path.exists(join(bugs_build, 'compile_commands.json')):
        # find llvm_src dir so we can figure out where clang #includes are for btrace
        llvm_src = None
#        for line in open(os.path.realpath(os.getcwd() + "/../src_clang/config.mak")):
        config_mak = project['lava'] + "/src_clang/config.mak"
        print "config.mak = [%s]" % config_mak
        for line in open(config_mak):
            foo = re.search("LLVM_SRC_PATH := (.*)$", line)
            if foo:
                llvm_src = foo.groups()[0]
                break
        assert(not (llvm_src is None))

        print "lvm_src = %s" % llvm_src

        run([join(lava_dir, 'btrace', 'sw-btrace-to-compiledb'), llvm_src + "/Release/lib/clang/3.6.1/include"])
#                '/home/moyix/git/llvm/Debug+Asserts/lib/clang/3.6.1/include'])
        # also insert instr for main() fn in all files that need it
        print "Instrumenting main fn by running lavatool on %d files\n" % (len(main_files))
        for f in main_files:
            print "injecting lava_set and lava_get code into [%s]" % f
            instrument_main(f)
            run(['git', 'add', f])
        run(['git', 'add', 'compile_commands.json'])
        run(['git', 'commit', '-m', 'Add compile_commands.json and instrument main.'])
        run(shlex.split(project['make']))
        try:
            run(shlex.split("find .  -name '*.[ch]' -exec git add '{}' \\;"))
        except subprocess32.CalledProcessError:
            pass
        run(['git', 'commit', '-m', 'Add compile_commands.json and instrument main.'])
        if not os.path.exists(bugs_install):
            run(project['install'], shell=True)

        # ugh binutils readelf.c will not be lavaTool-able without
        # bfd.h which gets created by make.
        run_cmd_nto(project["make"], bugs_build, None)
        run(shlex.split("find .  -name '*.[ch]' -exec git add '{}' \\;"))
        try:
            run(['git', 'commit', '-m', 'Adding any make-generated source files'])
        except subprocess32.CalledProcessError:
            pass

    # Now start picking the bug and injecting
    bugs_to_inject = []
    if args.bugid != -1:
        bug_id = int(args.bugid)
        score = 0
        bugs_to_inject.append(db.session.query(Bug).filter_by(id=bug_id).one())
    elif args.randomize:
        print "Remaining to inj:", db.uninjected().count()
        print "Using strategy: random"
#        (bug_id, dua_id, atp_id, inj) = next_bug_random(project, True)
        bugs_to_inject.append(db.next_bug_random())
        update_db = True
    elif args.buglist:
        buglist = eval(args.buglist)
        bugs_to_inject.append(
            db.session.query(Bug).filter(Bug.id.in_(buglist)).all()
        )
        update_db = False
    elif args.many:
        num_bugs_to_inject = int(args.many)
        print "Injecting %d bugs" % num_bugs_to_inject
        for i in range(num_bugs_to_inject):
            bugs_to_inject.append(db.next_bug_random())
        # NB: We won't be updating db for these bugs
#        update_db = True
    else: assert False
    print "bugs to inject:", bugs_to_inject

    # collect set of src files into which we must inject code
    src_files = set()
    i = 0

    for bug_index, bug in enumerate(bugs_to_inject):
         print "------------\n"
         print "SELECTED BUG {} : {}".format(bug_index, bug.id)#
 ####        if not args.randomize: print "   score=%d " % score
         print "   (%d,%d)" % (bug.dua.id, bug.atp.id)
         print "DUA:"
         print "   ", bug.dua
         print "ATP:"
         print "   ", bug.atp
         print "max_tcn={}  max_liveness={}".format(
             bug.dua.max_liveness, bug.dua.max_tcn)
         src_files.add(bug.dua.lval.file)
         src_files.add(bug.atp.file)

    # cleanup
    print "------------\n"
    print "CLEAN UP SRC"
    run_cmd_nto("/usr/bin/git checkout -f", bugs_build, None)

    print "------------\n"
    print "INJECTING BUGS INTO SOURCE"
    print "%d source files: " % (len(src_files))
    print src_files
    for src_file in src_files:
        print "inserting code into dua file %s" % src_file
        offset = 0
        if src_file in main_files:
            offset = NUM_LINES_MAIN_INSTR
        (exitcode, output) = inject_bugs_into_src(bugs_to_inject, src_file, offset)
        # note: now that we are inserting many dua / atp bug parts into each source, potentially.
        # which means we can't have simple exitcodes to indicate precisely what happened
        print "exitcode = %d" % exitcode
        if exitcode != 0:
            print output[1]

    # ugh -- with tshark if you *dont* do this, your bug-inj source may not build, sadly
    # it looks like their makefile doesn't understand its own dependencies, in fact
    if ('makeclean' in project) and (project['makeclean']):
        run_cmd_nto("make clean", bugs_build, None)

    # compile
    print "------------\n"
    print "ATTEMPTING BUILD OF INJECTED BUG"
    print "build_dir = " + bugs_build
    (rv, outp) = run_cmd_nto(project['make'], bugs_build, None)
    build = Build(compile=(rv == 0), output=(outp[0] + ";" + outp[1]))
    if rv!=0:
        # build failed
        print outp
        print "build failed"
        sys.exit(1)
    else:
        # build success
        print "build succeeded"
        (rv, outp) = run_cmd_nto("make install", bugs_build, None)
        assert rv == 0 # really how can this fail if build succeeds?
        print "make install succeeded"

    # add a row to the build table in the db
    if update_db:
        db.session.add(build)

    try:
        # build succeeded -- testing
        print "------------\n"
        # first, try the original file
        print "TESTING -- ORIG INPUT"
        orig_input = join(top_dir, 'inputs', basename(bug.dua.inputfile))
        (rv, outp) = run_prog(bugs_install, orig_input, timeout)
        if rv != 0:
            print "***** buggy program fails on original input!"
            assert False
        else:
            print "buggy program succeeds on original input"
        print "retval = %d" % rv
        print "output:"
        lines = outp[0] + " ; " + outp[1]
#            print lines
        if update_db:
            db.session.add(Run(build=build, fuzzed=None, exitcode=rv,
                               output=lines, success=True))
        print "SUCCESS"
        # second, fuzz it with the magic value
        print "TESTING -- FUZZED INPUTS"
        suff = get_suffix(orig_input)
        pref = orig_input[:-len(suff)] if suff != "" else orig_input
        real_bugs = []
        for bug_index, bug in enumerate(bugs_to_inject):
            fuzzed_input = "{}-fuzzed-{}{}".format(pref, bug.id, suff)
            print bug
            print "fuzzed = [%s]" % fuzzed_input
            mutfile(orig_input, bug.dua.labels, fuzzed_input, bug.id)
            print "testing with fuzzed input for {} of {} potential.  ".format(
                bug_index + 1, len(bugs_to_inject))
            print "{} real. bug {}".format(len(real_bugs), bug.id)
            (rv, outp) = run_prog(bugs_install, fuzzed_input, timeout)
            print "retval = %d" % rv
            print "output:"
            lines = outp[0] + " ; " + outp[1]
#                print lines
            if update_db:
                db.session.add(Run(build=build, fuzzed=bug, exitcode=rv,
                                output=lines, success=True))
            if rv == -11 or rv == -6:
                real_bugs.append(bug_id)
            print
        f = float(len(real_bugs)) / len(bugs_to_inject)
        print "yield {:.2f} ({} out of {}) real bugs".format(
            f, len(real_bugs), len(bugs_to_inject)
        )
        print "TESTING COMPLETE"
        if len(bugs_to_inject) > 1:
            print "list of real validated bugs:", real_bugs
        # NB: at the end of testing, the fuzzed input is still in place
        # if you want to try it
    except Exception as e:
        print "TESTING FAIL"
        if update_db:
            db.session.add(Run(build=build, fuzzed=None, exitcode=None,
                               output=str(e), success=False))
        raise e

    print "inject complete %.2f seconds" % (time.time() - start_time)
