#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import bz2
from collections import defaultdict
import json
import operator
from praw.handlers import MultiprocessHandler
import praw

import random
import re
import time
import urllib.request

try:
    from credentials import *  # NOQA
except:
    USERNAME = 'someuser'
    PASSWORD = 'somepass'
    USERAGENT = 'someuseragent'
    CACHEFILE = '/path/to/cachefile'
    DATABASEFILE = '/path/to/databasefile'
    REPORT_SUBREDDIT = 'somesubreddit'
    IGNORED_SUBREDDITS = (
        '''
        somesubreddit
        '''.lower().split())
    IGNORED_USERS = (
        '''
        someuser
        '''.lower().split())


def p(data, end='\n', color_seed=None):
    if color_seed:
        random.seed(color_seed)
        color = '\033[0;3{}m'.format(random.randint(1, 6))
    else:
        color = ''
    print(time.strftime(
        '\r\033[K\033[2K[\033[31m%y\033[39m/\033[31m%m\033[39m/\033[31m%d'
        '\033[39m][\033[31m%H\033[39m:\033[31m%M\033[39m:\033[31m%S\033[39m] ')
        + color + data + '\033[39m', end=end)


def cache_url():
    """Url caching decorator.  For decorating class functions that take a single url as an arg
    and return the response."""

    def wrap(function):
        def new_function(*args):
            url = args[1]
            expire_after = args[0].cache_time
            try:
                with bz2.open(CACHEFILE, 'rt') as f:
                    d = json.loads(f.read())
            except (IOError, ValueError):
                d = dict()
            if 'cache' not in d:
                d['cache'] = dict()
            if url in d['cache']:
                output = d['cache'][url]
                expire_time = output['time'] + expire_after
                if expire_after == 0 or time.time() < expire_time:
                    return output['data']
                else:
                    del d['cache'][url]
            output = function(*args)
            if output:
                to_cache = {'time': time.time(), 'data': output}
                d['cache'][url] = to_cache
                with bz2.open(CACHEFILE, 'wt') as f:
                    f.write(json.dumps(d))
                return output
        return new_function
    return wrap


class Youtube(object):
    def __init__(self, cache_time=0):
        self.opener = urllib.request.build_opener()
        self.opener.addheaders = [('User-agent', USERAGENT)]
        self.last_request = 0
        self.cache_time = cache_time

    @cache_url()
    def _request(self, url):
        try:
            since_last = time.time() - self.last_request
            if not since_last >= 2:
                time.sleep(2 - since_last)
            with self.opener.open(url, timeout=30) as w:
                youtube = w.read().decode('utf-8')
                yt_json = json.loads(youtube)
        except:
            self.last_request = time.time()
            return None

        if 'errors' not in yt_json:
            return yt_json['entry']

    def _get_id(self, url):
        # regex via: http://stackoverflow.com/questions/3392993/php-regex-to-get-youtube-video-id
        regex = re.compile(
            r'''(?<=(?:v|i)=)[a-zA-Z0-9-]+(?=&)|(?<=(?:v|i)\/)[^&\n]+|(?<=embed\/)[^"&\n]+|'''
            r'''(?<=(?:v|i)=)[^&\n]+|(?<=youtu.be\/)[^&\n]+''', re.I)
        yt_id = regex.findall(
            url.replace('%3D', '=').replace('%26', '&').replace('%2F', '?').replace('&amp;', '&'))

        if yt_id:
            # temp fix:
            yt_id = yt_id[0].split('#')[0]
            yt_id = yt_id.split('?')[0]
            return yt_id

    def _get(self, url):
        """Decides if we're grabbing video info or a profile."""
        urls = {
            'profile': 'http://gdata.youtube.com/feeds/api/users/{}?v=2&alt=json',
            'video': 'http://gdata.youtube.com/feeds/api/videos/{}?v=2&alt=json'}

        yt_id = self._get_id(url)

        if yt_id:
            return self._request(urls['video'].format(yt_id))
        else:
            username = re.findall(r'''(?i)\.com\/(?:user\/|channel\/)?(.*?)(?:\/|\?|$)''', url)
            if username:
                return self._request(urls['profile'].format(username[0]))

    def get_author(self, url):
        """Returns the author id of the youtube url"""
        output = self._get(url)
        if output:
            # There has to be a reason for the list in there...
            return output['author'][0]['yt$userId']['$t']

    def get_info(self, url):
        """Returns the title and description of a video."""
        output = self._get(url)
        if output:
            if 'media$group' in output:
                title = output['title']['$t']
                description = output['media$group']['media$description']['$t']
                return {'title': title, 'description': description}

    def is_video(self, url):
        if self._get_id(url) is not None:
            return True
        else:
            return False


class Filter(object):
    """Base filter class"""
    def __init__(self):
        self.regex = None
        self.tag = ""
        self.log_text = ""
        self.report_subreddit = None
        self.nuke = True
        self.reddit = None

    def filterSubmission(self, submission):
        raise NotImplementedError

    def runFilter(self, post):
        if 'title' in vars(post):
            try:
                if self.filterSubmission(post):
                    return True
            except NotImplementedError:
                pass


class YoutubeSpam(Filter):
    def __init__(self, reddit, youtube):
        Filter.__init__(self)
        self.tag = "[Youtube Spam]"
        self.reddit = reddit
        self.y = youtube

    def _isVideo(self, submission):
        '''Returns video author name if this is a video'''
        if submission.domain in ('m.youtube.com', 'youtube.com', 'youtu.be'):
            return self.y.get_author(submission.url)

    def _checkProfile(self, submission):
        '''Returns the percentage of things that the user only contributed to themselves.
        ie: submitting and only commenting on their content.  Currently, the criteria is:
            * linking to videos of the same author (which implies it is their account)
            * commenting on your own submissions (not just videos)
        these all will count against the user and an overall score will be returned.  Also, we only
        check against the last 100 items on the user's profile.'''

        try:
            start_time = time.time() - (60 * 60 * 24 * 30 * 6)  # ~six months
            redditor = self.reddit.get_redditor(submission.author.name)
            comments = [i for i in redditor.get_comments(limit=100) if i.created_utc > start_time]
            submitted = [i for i in redditor.get_submitted(limit=100) if i.created_utc > start_time]
        except urllib.error.HTTPError:
            # This is a hack to get around shadowbanned or deleted users
            p("Could not parse /u/{}, probably shadowbanned or deleted".format(user))
            return False
        video_count = defaultdict(lambda: 0)
        video_submissions = set()
        comments_on_self = 0
        initial_author = self._isVideo(submission)
        for item in submitted:
            video_author = self._isVideo(item)
            if video_author:
                video_count[video_author] += 1
                video_submissions.add(item.name)
        if video_count:
            most_submitted_author = max(video_count.items(), key=operator.itemgetter(1))[0]
        else:
            return False
        for item in comments:
            if item.link_id in video_submissions:
                comments_on_self += 1
        try:
            video_percent = max(
                [video_count[i] / sum(video_count.values()) for i in video_count])
        except ValueError:
            video_percent = 0
        if video_percent > .85 and sum(video_count.values()) >= 3:
            spammer_value = (sum(video_count.values()) + comments_on_self) / (len(
                comments) + len(submitted))
            if spammer_value > .85 and initial_author == most_submitted_author:
                return True

    def filterSubmission(self, submission):
        self.report_subreddit = None
        if submission.domain in ('m.youtube.com', 'youtube.com', 'youtu.be'):
            # check if we've already parsed this submission
            try:
                with bz2.open(DATABASEFILE, 'rt') as db:
                    db = json.loads(db.read())
            except IOError:
                db = dict()
                db['users'] = dict()
                db['submissions'] = list()

            if submission.id in db['submissions']:
                return False
            if submission.author.name in db['users']:
                user = db['users'][submission.author.name]
            else:
                user = {'checked_last': 0, 'reported': False}

            if not user['reported']:
                p("Checking profile of /u/{}".format(submission.author.name), end='')
                if self._checkProfile(submission):
                    self.log_text = "Found video spammer"
                    p(self.log_text + ":")
                    p("http://reddit.com/u/{}".format(submission.author.name),
                        color_seed=submission.author.name)
                    self.report_subreddit = REPORT_SUBREDDIT
                    user['reported'] = True
                    output = True
                else:
                    output = False
                db['users'][submission.author.name] = user
                db['submissions'].append(submission.id)
                with bz2.open(DATABASEFILE, 'wt') as f:
                    f.write(json.dumps(db))
                return output


def get_listings(reddit, stop_point):
    subreddit = reddit.get_subreddit('all')
    all_listings = [i for i in subreddit.get_new(limit=1000, place_holder=stop_point)]
    listings = []
    for thing in all_listings:
        if thing.subreddit.display_name.lower() not in IGNORED_SUBREDDITS:
            if thing.author and thing.author.name.lower() not in IGNORED_USERS:
                listings.append(thing)
    return listings


def main():
    r = praw.Reddit(USERAGENT, handler=MultiprocessHandler())
    r.login(USERNAME, PASSWORD)
    stop_point = ''
    yt_spam = YoutubeSpam(r, Youtube(cache_time=0))
    p('Started monitoring submissions on /r/all-{}.'.format('-'.join(IGNORED_SUBREDDITS)))
    start_time = time.time()

    # Main Loop
    while True:
        sleep_time = 60
        listings = get_listings(r, stop_point)
        for thing in listings:
            p('Processing {}'.format(thing.id), color_seed=thing.name, end='')
            if yt_spam.runFilter(thing):
                if yt_spam.report_subreddit:
                    r.submit(
                        yt_spam.report_subreddit,
                        '{} {}'.format(thing.author.name, yt_spam.tag),
                        url=thing.author._url)
            stop_point = thing.id
        end_time = time.time()
        total_time = end_time - start_time
        if total_time >= sleep_time:
            sleep_time = 1
        for i in range(sleep_time):
            p('Next scan in {} seconds...'.format(sleep_time - i), end='')
            time.sleep(1)

if __name__ == '__main__':
    main()
