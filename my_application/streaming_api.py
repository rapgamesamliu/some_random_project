'''
streaming_api.py

Written May 17-20, 2011 by Josiah Carlson
Released under the GNU GPL v2
available: http://www.gnu.org/licenses/gpl-2.0.html

Other licenses may be available upon request.

Given a Redis server and a task queue implementation, this code implements the
equivalent of Twitter's streaming API as specified here:
 http://dev.twitter.com/pages/streaming_api_methods
with a few differences:
1. Usernames are followed instead of user ids when using the follow call.
2. The attributes used for various items (retweeted_by, author, created_at)
   aren't necessarily the same names or the same types as what Twitter uses.
3. Client access restrictions, limitations, etc., are not handled here.

The primary goals of this implementation is to allow for workers to process as
fast as possible, but still "work" if they are unable to keep up, or if the
client they are working for is unable to receive data fast enough.

'''

import itertools
import json
import os
import random
import time

ID_KEY = 'seq:id'
QUEUE = 'message:queue'
STATUS_MESSAGE = 'messsage:%s'

CHUNKSIZE = 100
MAX_BACKLOG = 150000
MAX_OUTGOING_BACKLOG = 5 * CHUNKSIZE
NO_MESSAGES_WAIT = .1
KEEPALIVE_TIMEOUT = 30

def got_status(conn, status, id=None):
    '''
    This will work until there are 2**53 ids generated, then we may get
    duplicate messages sent to the workers. There are some work-arounds, but
    they confuse the clean flow of the existing code.

    This function takes a Redis connection object, a status message, and an
    optional id. If the id is not None, the status message is assumed to be
    pre-dumped to json. If the id is None, the status will have a new id
    assigned to it, along with the current timestamp in seconds since the
    standard unix epoch.
    '''
    dumped = status
    if id is None:
        id = conn.incr(ID_KEY)
        status['id'] = id
        status['created_at'] = time.time()
        dumped = json.dumps(status)

    pipeline = conn.pipeline(True) # a pipeline returns itself
    pipeline.zadd(QUEUE, id, id)
    pipeline.set(STATUS_MESSAGE%(id,), dumped)
    pipeline.execute()
    return id

def spawn_worker_and_subscribe(which, content=None, backlog=0):
    '''
    This would be called by a web server to connect to some Redis server that
    is holding all of the status messages and data, yielding results as they
    become available.

    This function requires two utility functions be present:
    get_new_redis_connection(which):
        This will create or reuse a connection to some Redis server that is
        hosting the status messages.
    spawn_worker(...):
        This will spawn the worker() function above on some worker box
        somewhere, pushing matched status messages to the client via a list
        named sub:...
    '''
    conn = get_new_redis_connection(which)
    channel = 'sub:' + os.urandom(16).encode('hex')
    spawn_worker(conn.hostinfo, backlog, which, content, channel)

    while True:
        result = conn.blpop(channel, timeout=60)
        if result in (None, '<close>'):
            break
        yield result

def worker(hostinfo, backlog, which, content, subscriber):
    '''
    This worker handles the scanning of status message content against the
    user-requested filters.
    '''
    criteria = None
    if which == 'track':
        # should be a comma separated list of word strings
        # Given: 'streamapi,streaming api'
        # The first will match any status with 'streamapi' as an individual
        # word. The second will match any status with 'streaming' and 'api'
        # both in the status as individual words.
        criteria = TrackCriteria(content)
    elif which == 'follow':
        # should be a list of @names without the @
        criteria = FollowCriteria(content)
    elif which == 'location':
        # should be a list of boxes: [{'minlat':..., 'maxlat':..., ...}, ...]
        criteria = LocationCriteria(content)
    elif which == 'firehose':
        criteria = lambda status: True
    elif which == 'gardenhose':
        criteria = lambda status: not random.randrange(10)
    elif which == 'spritzer':
        criteria = lambda status: not random.randrange(100)
    elif which == 'links':
        criteria = lambda status: status['has_link']

    conn = get_new_redis_connection(hostinfo)
    if criteria is None:
        conn.rpush(subscriber, '<close>')
        return

    # set up the backlog stuff
    end = 'inf'
    now = int(conn.get(ID_KEY) or 0)
    if backlog < 0:
        end = now
    now -= abs(backlog)

    pipeline = conn.pipeline(False)
    sent = 1
    tossed = tossed_notified = 0
    keepalive = time.time() + KEEPALIVE_TIMEOUT
    last_sent_keepalive = False
    # In Python 2.x, all ints/longs compare smaller than strings.
    while sent and now < end:
        found_match = False
        # get the next set of messages to check
        ids = conn.zrangebyscore(QUEUE, now, end, start=0, num=CHUNKSIZE)
        if ids:
            # actually pull the data
            for id in ids:
                pipeline.get(STATUS_MESSAGE%(id,))
            pipeline.llen(subscriber)
            statuses = pipeline.execute()
            outgoing_backlog = statuses.pop()

            for data in statuses:
                if not data:
                    # We weren't fast enough, someone implemented delete and
                    # the message is gone, etc.
                    continue
                result = json.loads(data)
                # check the criteria
                if criteria(result):
                    if outgoing_backlog >= MAX_OUTGOING_BACKLOG:
                        tossed += 1
                        continue
                    # send the result to the subscriber
                    last_sent_keepalive = False
                    if tossed_notified != tossed:
                        pipeline.rpush(subscriber, json.dumps({"limit":{which:tossed}}))
                        tossed_notified = tossed
                    outgoing_backlog += 1
                    found_match = True
                    pipeline.rpush(subscriber, data)

            if found_match:
                keepalive = time.time() + KEEPALIVE_TIMEOUT
                sent = any(pipeline.execute())
            # update the current position in the zset
            now = int(ids[-1])

        elif end == 'inf':
            time.sleep(NO_MESSAGES_WAIT)

        else:
            # we have exhausted the backlog stream
            break

        curtime = time.time()
        if not found_match and curtime > keepalive:
            keepalive = curtime + KEEPALIVE_TIMEOUT
            should_quit = last_sent_keepalive and conn.llen(subscriber)
            should_quit = should_quit or conn.rpush(subscriber, '{}') >= MAX_OUTGOING_BACKLOG
            if should_quit:
                # Can't keep up even though it's been 30 seconds since we saw
                # a match. We'll kill the queue here, and the client will time
                # out on a blpop() call if it retries.
                conn.delete(subscriber)
                break
            last_sent_keepalive = True

def replicator(source_conn, dest_conn):
    '''
    This is to perform zero-slave replication for when a single Redis server
    is not fast enough to handle clients pulling the tweets.
    '''
    pipeline = source_conn.pipeline(False)
    sleep = .001
    then = int(source_conn.get(ID_KEY) or '0')
    while True:
        now = int(source_conn.get(ID_KEY) or '0')
        if now == then:
            sleep = min(.1, 2 * sleep)
            time.sleep(sleep)
            continue
        sleep = .001
        # We use a non-transactional pipeline so that this request doesn't
        # overload the Redis server when we are syndicating out a large volume
        # of status messages.
        for id in xrange(then+1, now+1):
            pipeline.get(STATUS_MESSAGE%(id,))
        for id, status in itertools.izip(xrange(then+1, now+1), pipeline.execute()):
            got_status(dest_conn, status, id)
        then = now

def cleaner(conn):
    '''
    At least one of these should be run on any given Redis server that is
    hosting the streaming data.
    '''
    pipeline = conn.pipeline(False)
    while True:
        to_delete = conn.zrange(0, -MAX_BACKLOG)
        for item in to_delete:
            pipeline.zrem(QUEUE, item)
            pipeline.delete(STATUS_MESSAGE%(item,))
        pipeline.execute()
        time.sleep(1)

class TrackCriteria:
    def __init__(self, list_of_ors):
        self.words = [set(words.lower().split()) for words in list_of_ors.split(',')]
    def __call__(self, status):
        content = set(status['text'].lower().split())
        for setx in self.words:
            if len(content & setx) == len(setx):
                return True
        return False

class FollowCriteria:
    def __init__(self, people):
        # This differs from the twitter-based version in that it follows
        # names and not user ids.
        self.people = set(user.lower() for user in people.split(','))
    def __call__(self, status):
        matched = (status['author'].lower() in self.people or
            status.get('retweeted_by', '').lower() in self.people)
        return matched

class LocationCriteria:
    def __init__(self, boxes):
        boxes = boxes.split(',')
        self.boxes = []
        for i in xrange(0, len(boxes)-3, 4):
            self.boxes.append(map(float, boxes[i:i+4]))
    def __call__(self, status):
        if status['geo']:
            for box in self.boxes:
                if (box[0] <= status['geo']['lon'] <= box[1] and
                    box[2] <= status['geo']['lat'] <= box[3]):
                    return True
        return False
