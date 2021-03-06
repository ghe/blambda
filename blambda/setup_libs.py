import sys
import json
import argparse
import os
import stat
import subprocess
import shutil
from tempfile import NamedTemporaryFile

from .utils.base import pGreen, pRed, pBlue, spawn
from .utils.install import install_deps
from .utils.findfunc import (
    find_manifest,
    split_path
)


def make_local_activate(basedir, include_dir, clean=False):
    """ set up a base lambda ve if needed, copy it locally to have packages installed there
    Args:
        basedir (str): directory to create the ve under
        libdir (str): where to install pip packages
        clean (bool): whether to delete existing dirs first
    """
    this_dir = os.path.dirname(os.path.abspath(__file__))
    py_dir = os.path.join(this_dir, "python")
    master_ve_dir = os.path.join(py_dir, "lambdave")
    master_activate = os.path.abspath(os.path.join(master_ve_dir, "activate"))
    local_ve_dir = os.path.join(basedir, "ve_{}".format(os.path.basename(basedir)))
    include_dir = os.path.join(basedir, "lib")
    module_dir = os.path.join(basedir, "node_modules")
    local_activate = os.path.abspath(os.path.join(local_ve_dir, "bin", "activate"))
    base_activate = os.path.abspath(os.path.join(basedir, "activate"))

    if clean:
        for path in [include_dir, local_ve_dir, base_activate]:
            if os.path.exists(path):
                print(pBlue("Clean: removing {}".format(path)))
                if os.path.isdir(path):
                    shutil.rmtree(path)
                else:
                    os.remove(base_activate)

    #copy the base local ve, but update the activate file with the new path
    if not os.path.exists(local_ve_dir):
        if not os.path.exists(master_ve_dir):
            print("no master lambda VE. Let me make that for you")
            (r, s, e) = spawn("makeLambdaVE", workingDirectory=py_dir, show=True)
        pyexecpath = os.path.join(master_ve_dir, "python", "install", "bin", "python")
        vebasepath = os.path.join(master_ve_dir, "virtualenv", "src")
        versiondir = [p for p in os.listdir(vebasepath) if not p.endswith("tar.gz")][0]
        veexecpath = os.path.join(vebasepath, versiondir, "virtualenv.py")
        #$PYTHON virtualenv.py $VEDIR -p $PYTHON --no-site-packages".format
        cmd = "{python} {vescript} {vedir} -p {python} --no-site-packages".format(
            python=pyexecpath,
            vescript=veexecpath,
            vedir=os.path.basename(local_ve_dir)
        )
        (r, s, e) = spawn(cmd, workingDirectory=basedir, show=True)

        #find all files that refer to the absolute path in the shebang
        #and replace them with /bin/env python
        searchpath = os.path.join(local_ve_dir, "bin")
        local_pyexecpath = os.path.join(searchpath, "python")
        for fname in os.listdir(searchpath):
            fname = os.path.join(searchpath, fname)
            if os.path.isfile(fname):
                with open(fname) as f:
                    data = f.readlines()
                try:
                    if local_pyexecpath in data[0]:
                        with open(fname, "w") as f:
                            f.write("\n".join(["#!/usr/bin/env python"] + data[1:]))
                except:
                    pass

        print(pGreen("\n**NOTE** Source 'activate' in your project root to ensure a proper environment\n"))

    if not os.path.exists(base_activate):
        with open(base_activate, "w") as f:
            f.write(". {}\n".format(os.path.abspath(local_activate)))
            f.write("export PYTHONPATH={}\n".format(os.path.abspath(include_dir)))

    local_python = os.path.abspath(os.path.join(local_ve_dir, "bin", "python"))
    if not os.path.exists(local_python):
        raise OSError("virtual env python not found at {}".format(local_python))
    return (local_activate, local_python)

def process_manifest(manifest_filename, basedir, clean, verbose=False):
    """ loads a manifest file, executes pre and post hooks and installs dependencies
    Args:
      manifest_filename (str): filename (+path) of the manifest
      basedir (str): directory to create the dependency dir under and execute hook commands in
      verbose (bool): inundates you with a deluge of information useful for investigating issues
    """
    manifest = {}
    with open(manifest_filename) as f:
        manifest = json.load(f)
    for command in manifest.get('before setup', []):
        spawn(command, show=True, workingDirectory=basedir, raise_on_fail=True)
    if 'dependencies' in manifest:
        good = install_deps(manifest['dependencies'], basedir, version_required=True, clean=clean, verbose=verbose)
        print(pGreen("All deps installed") if good else pRed("Failed to install one or more deps"))
        if not good:
            raise Exception("Failed to install one or more deps")
    for source in manifest['source files']:
        if type(source) in (tuple, list):
            (src, dst) = source
            if not os.path.exists(os.path.join(basedir, dst)):
                spawn("ln -s {} {}".format(src, dst), show=True, workingDirectory=basedir, raise_on_fail=True)
            else:
                print("Not (re)linking {} to {}, destination exists".format(src, dst))
    for command in manifest.get('after setup', []):
        spawn(command, show=True, workingDirectory=basedir, raise_on_fail=True)

def run_from_ve(activate_script, fname):
    """ Run ourselves from the lambda-equivalent ve for this function
    this causes the pip deps to be installed for this specific ve, making
    local tests run with only the deps from the manifest
    Args:
        activate_script (str): path to the desired ve's activation script
        fname (str): the function name we're setting up the deps for
    """
    tmpfile = NamedTemporaryFile(delete=False)
    with open(tmpfile.name, 'w') as tmp:
        tmp.write("#!/bin/sh\n")
        tmp.write(". {}\n".format(activate_script))
        tmp.write("echo now using py: $(which python)\n")
        tmp.write("echo now using pip: $(which pip)\n")
        #upgrading pip breaks the paths again
        tmp.write("#pip install --upgrade pip\n")
        tmp.write("pip install futures\n")
        tmp.write("pip install boto3\n")
        tmp.write("{} {} --recursive\n".format(os.path.abspath(__file__), fname))
    #make the script executable
    os.chmod(tmp.name, 0777)
    tmpfile.file.close()
    subprocess.call(tmpfile.name)

def main(args=None):
    parser = argparse.ArgumentParser("prepare development of python lambda functions")
    parser.add_argument('function_names', nargs='*', type=str, help='the base name of the function')
    parser.add_argument('--nove', help='skip setting up a virtual environment', action='store_true')
    parser.add_argument('-v', '--verbose', help='verbose output', action='store_true')
    parser.add_argument('-c', '--clean', help='clean environment', action='store_true')
    parser.add_argument('--recursive', help=argparse.SUPPRESS, action='store_true')
    parser.add_argument('--file', type=str, help='filename containing function names')
    args = parser.parse_args(args)

    fnames = args.function_names
    if args.file:
        if os.path.isfile(args.file):
            with open(args.file) as f:
                fnames += [l.strip() for l in f.readlines()]
            print("read {} from {}".format(fnames, args.file))
        else:
            print(pRed("unable to read from {}".format(args.file)))

    for fname in set(fnames):
        manifest_filename = find_manifest(fname)
        if manifest_filename:
            print(pBlue("setting up {}".format(fname)))
            (basedir, name, ext) = split_path(manifest_filename)
            if args.nove or 'node' in basedir:
                #setting up the libs in whatever env we're in
                process_manifest(manifest_filename, basedir, args.clean, args.verbose)
            else:
                incdir = os.path.join(basedir, "lib")
                (activate_script, python_executable) = make_local_activate(basedir, incdir, args.clean)
                if sys.executable == python_executable:
                    # already running in the right ve!
                    if args.verbose:
                        print("processing manifest from ve {}".format(activate_script))
                    process_manifest(manifest_filename, basedir, args.clean, args.verbose)
                else:
                    if args.verbose:
                        print("no exec match: {} vs {}".format(sys.executable, python_executable))
                    if args.recursive:
                        raise Exception("Failed to enter virtual environment!")
                    print("entering ve")
                    run_from_ve(activate_script, fname)
        else:
            print(pRed("unable to find {}".format(fname)))

if __name__ == '__main__':
    main()
