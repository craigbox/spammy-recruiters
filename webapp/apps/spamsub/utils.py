"""
Utility functions for interacting with our Git repos
"""
import os
import json
from datetime import datetime, timedelta

from webapp import app
from flask import abort, flash, render_template
from sqlalchemy import func, desc
from models import *
from models import utcnow as utcnow_
from git import Repo
from git.exc import *
from requests.exceptions import HTTPError
import requests
import humanize

basename = os.path.dirname(__file__)
now = datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S")
repo = Repo(os.path.join(basename, "git_dir"))


def ok_to_update():
    """ If we've got more than two new addresses, or a day's gone by """
    counter = Counter.query.first()
    if not counter:
        counter = Count(0)
        db.session.add(counter)
        db.session.commit()
    elapsed = counter.timestamp - datetime.utcnow()
    return any([counter.count >= 2, abs(elapsed.total_seconds()) >= 86400])

def check_if_exists(address):
    """
    Check whether a submitted address exists in the DB, add it if not,
    re-generate the spammers.txt file, and open a pull request with the updates
    """
    normalised = u"@" + address.lower().strip()
    # add any missing spammers to our DB
    if Address.exists(normalised):
        # if we immediately find the address, don't continue
        return True
    else:
        # otherwise, pull updates from GitHub, and check again
        update_db()
    if not Address.exists(normalised):
        db.session.add(Address(address=normalised))
        count = Counter.query.first()
        if not count:
            count = Counter(0)
        count.count += 1
        db.session.add(count)
        db.session.commit()
        if ok_to_update():
            write_new_spammers()
        return False
    return True

def write_new_spammers():
    """
    Synchronise all changes between GitHub and webapp
    Because we may have multiple pending pull requests,
    each changeset must be added to a new integration branch, which issues
    the pull request to origin/master

    TODO: tidy up remote integration branches
    """
    errs = False
    # pull all branches from origin, and force-checkout master
    repo_checkout()
    # switch to a new integration branch
    newbranch = "integration_%s" % datetime.utcnow().strftime(
            "%Y_%b_%d_%H_%M_%S")
    git.checkout(b=newbranch)
    index = repo.index
    # re-generate spammers.txt
    try:
        output(filename="spammers.txt")
    except IOError as e:
        app.logger.error("Couldn't write spammers.txt. Err: %s" % e)
        repo.heads.master.checkout(f=True)
        git.branch(newbranch, D=True)
        errs = True
    # add spammers.txt to local integration branch
    try:
        index.add(['spammers.txt'])
        index.commit("Updating Spammers on %s" % now)
        # push local repo to webapp's remote
        our_remote.push(newbranch)
    except GitCommandError as e:
        errs = True
        app.logger.error("Couldn't push to staging remote. Err: %s" % e)
    # send pull request to main remote
    our_sha = "urschrei:%s" % newbranch
    their_sha = 'master'
    if not errs and pull_request(our_sha, their_sha):
        # delete our local integration branch, and reset counter
        counter = Counter.query.first()
        counter.count = 0
        counter.timestamp = utcnow_()
        db.session.add(counter)
        db.session.commit()
        git.checkout("master")
        git.branch(newbranch, D=True)
    else:
        # register an error
        errs = True
    if errs:
        flash(
            "There was an error sending your updates to GitHub. We'll \
try again later, though, and they <em>have</em> been saved.", "text-error"
            )

def output(filename, template="output.jinja"):
    """ write filename to the git directory, using the specified template """
    with open(os.path.join(basename, "git_dir", filename), "w") as f:
            f.write(render_template(
                template,
                addresses=[record.address.strip() for
                    record in Address.query.order_by('address').all()]))

def get_spammers():
    """ Return an up-to-date list of spammers from the main repo text file """
    with open(os.path.join(basename, "git_dir", 'spammers.txt'), 'r') as f:
        spammers = f.readlines()
    # trim the " OR" and final newline from the entries
    # FIXME: this is a bit fragile
    return [spammer.split()[0] for spammer in spammers]

def pull_request(our_sha, their_sha):
    """ Open a pull request on the main repo """
    payload = {
        "title": "Updated Spammers on %s" % now,
        "body": "Updates from the webapp",
        "head": our_sha,
        "base": their_sha
    }
    headers = {
        "Authorization": 'token %s' % app.config['GITHUB_TOKEN'],
    }
    req = requests.post(
        "https://api.github.com/repos/drcongo/spammy-recruiters/pulls",
        data=json.dumps(payload), headers=headers)
    try:
        req.raise_for_status()
    except HTTPError as e:
        app.logger.error("Couldn't open pull request. Error: %s" % e)
        return False
    return True

def repo_checkout():
    """ Ensure that the spammers.txt we're comparing is from origin/master """
    git = repo.git
    try:
        git.pull(all=True)
        repo.heads.master.checkout(f=True)
        git.checkout("spammers.txt", f=True)
    except (GitCommandError, CheckoutError) as e:
        # Not much point carrying on without the latest spammer file
        app.logger.critical("Couldn't check out latest spammers.txt: %s" % e)
        abort(500)

def update_db():
    """ Add any missing spammers to our app DB """
    # pull all branches from origin, and force-checkout master
    repo_checkout()
    their_spammers = set(get_spammers())
    our_spammers = set(addr.address.strip() for addr in
        Address.query.order_by('address').all())
    to_update = list(their_spammers - our_spammers)
    if to_update:
        db.session.add_all([Address(address=new_addr) for
            new_addr in to_update])
    # reset sync timestamp
    latest = UpdateCheck.query.first() or UpdateCheck()
    latest.timestamp = utcnow_()
    db.session.add(latest)
    db.session.commit()

def sync_check():
    """
    Syncing the local and remote repos is a relatively slow process;
    there's no need to do it more than once per hour, really
    """
    latest = UpdateCheck.query.first()
    if not latest:
        latest = UpdateCheck()
        db.session.add(latest)
        db.session.commit()
    elapsed = datetime.utcnow() - latest.timestamp
    if abs(elapsed.total_seconds()) > 3600:
        update_db()
        elapsed = datetime.utcnow() - timedelta(seconds=1)
    return humanize.naturaltime(elapsed)
