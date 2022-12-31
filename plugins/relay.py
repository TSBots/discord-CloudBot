from cloudbot import hook
from cloudbot.event import Event, EventType
import pika
from munch import Munch
import datetime
import json
import re
import logging
import uuid

from plugins.core.chan_track import get_users

MQ_HOST = "rabbitmq"
MQ_VHOST = "/"
MQ_USER = ""
MQ_PASS = ""
MQ_PORT = 5672
MQ_SSL = False
SEND_QUEUE = "IRCToDiscord-Dev"
RECV_QUEUE = "DiscordToIRC-Dev"
IRC_CONNECTION = "libera"
CHANNEL_MAP = {
    1: "#channel",
    2: "#channel1"
}
CHANNELS = list(CHANNEL_MAP.values())
QUEUE_CHECK_SECONDS = 1
QUEUE_SEND_SECONDS = 1
STALE_PERIOD_SECONDS = 600
RESPONSE_LIMIT = 3
SEND_LIMIT = 3
SEND_JOIN_PART_EVENTS = False

IRC_BOLD = ""
IRC_ITALICS = ""
IRC_NULL = ""

log = logging.getLogger("relay_plugin")

# limits connections we have to make per hook
send_buffer = []

def get_permission(event):
    try:
        users = get_users(event.conn)
        user = users.get(event.nick)
        if user:
            member = user.channels.get(event.chan) if user else None
            if member:
                modes = []
                for entry in member.status:
                    modes.append(entry.mode)
                return modes
    except Exception as e:
        log.warning(f"Unable to get permissiaons for {event.nick}: {e}")
    return []

def serialize(type_, event):
    data = Munch()

    # event data
    data.event = Munch()
    data.event.type = type_
    data.event.uuid = str(uuid.uuid4())
    data.event.time = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    data.event.content = getattr(event, "content", None) or getattr(event, "content_raw", None)
    data.event.target = event.target
    data.event.irc_raw = event.irc_raw
    data.event.irc_prefix = event.irc_prefix
    data.event.irc_command = event.irc_command
    data.event.irc_paramlist = event.irc_paramlist
    data.event.irc_ctcp_text = event.irc_ctcp_text

    # author data
    data.author = Munch()
    data.author.nickname = event.nick
    data.author.username = event.user
    data.author.mask = event.mask
    data.author.host = event.host
    data.author.permissions = get_permission(event)

    # server data
    data.server = Munch()
    data.server.name = event.conn.name
    data.server.nick = event.conn.nick
    data.server.channels = event.conn.channels

    # channel data
    data.channel = Munch()
    data.channel.name = event.chan

    as_json = data.toJSON()
    log.info(f"Serialized data: {as_json}")
    return as_json

def deserialize(body):    
    try:
        deserialized = Munch.fromJSON(body)
    except Exception as e:
        log.warning(f"Unable to Munch-deserialize incoming data: {e}")
        return

    time = deserialized.event.time
    if not time:
        log.warning(f"Unable to retrieve time object from incoming data")
        return
    if time_stale(time):
        log.warning(
            f"Incoming data failed stale check ({STALE_PERIOD_SECONDS} seconds)"
        )
        return

    log.info(f"Deserialized data: {body})")
    return deserialized

def time_stale(time):
    time = datetime.datetime.strptime(time, "%Y-%m-%d %H:%M:%S.%f")
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    if (now - time).total_seconds() > STALE_PERIOD_SECONDS:
        return True
    return False

def get_mq_connection():
    # blocking but what ya gonna do?
    try:
        parameters = pika.ConnectionParameters(
            MQ_HOST,
            MQ_PORT,
            MQ_VHOST,
            pika.PlainCredentials(MQ_USER, MQ_PASS)
        )
        return pika.BlockingConnection(parameters)
    except Exception as e:
        e = e or "No route to host" # dumb correction to a blank error
        log.warning(f"Unable to connect to RabbitMQ: {e}")

def publish(bodies):
    mq_connection = None

    try:
        mq_connection = get_mq_connection()
        if not mq_connection:
            log.warning(f"Unable to retrieve MQ connection - aborting publish: {bodies}")
            return
        mq_channel = mq_connection.channel()
        mq_channel.queue_declare(queue=RECV_QUEUE, durable=True)
        for body in bodies:
            mq_channel.basic_publish(
                exchange="", routing_key=SEND_QUEUE, body=body
            )

    except Exception as e:
        log.warning(f"Unable to publish body to queue {SEND_QUEUE}: {e}")

    if mq_connection:
        mq_connection.close()

def consume():
    mq_connection = None
    bodies = []

    try:
        mq_connection = get_mq_connection()
        if not mq_connection:
            log.warning(f"Unable to retrieve MQ connection - aborting consume")
            return bodies
        mq_channel = mq_connection.channel()
        mq_channel.queue_declare(queue=RECV_QUEUE, durable=True)
        
        checks = 0
        while checks < RESPONSE_LIMIT:
            body = get_ack(mq_channel)
            checks += 1
            if not body:
                break
            bodies.append(body)

    except Exception as e:
        log.warning(f"Unable to consume from queue {RECV_QUEUE}: {e}")

    if mq_connection:
        mq_connection.close()

    return bodies

def get_ack(channel):
    method, _, body = channel.basic_get(queue=RECV_QUEUE)
    if method:
        channel.basic_ack(method.delivery_tag)
        return body

def format_message_event(data):
    attachment_urls = ", ".join(data.event.attachments) if data.event.attachments else ""
    attachment_urls = f"{attachment_urls}" if attachment_urls else ""
    mangled_nick = mangle_nick(data.author.nickname)

    reply_string = ""
    if data.event.reply:
        reply_string = f"(replying to '{_get_trimmed_content(data.event.reply.content)}') "

    return f"{IRC_BOLD}[D]{IRC_BOLD} <{_get_permissions_label(data.author.permissions)}{mangled_nick}> {reply_string}{data.event.content} {attachment_urls}"

def format_message_edit_event(data):
    mangled_nick = mangle_nick(data.author.nickname)
    return f"{IRC_BOLD}[D]{IRC_BOLD} <{_get_permissions_label(data.author.permissions)}{mangled_nick}> {data.event.content} ** (message edited)"

def format_reaction_add_event(data):
    mangled_nick = mangle_nick(data.author.nickname)
    return f"{_get_permissions_label(data.author.permissions)}{mangled_nick} reacted with {data.event.emoji} to '{_get_trimmed_content(data.event.content)}'"

def _get_trimmed_content(content):
    return content[:30] + "..." if len(content) > 30 else content

def _get_permissions_label(permissions):
    if permissions.admin:
        return "**"
    if permissions.ban or permissions.kick:
        return "*"
    else:
        return ""

def mangle_nick(nick):
    new_nick = ""
    for char in nick:
        new_nick += f"{char}{IRC_NULL}"
    return new_nick

@hook.regex(re.compile(r'[\s\S]+'))
def irc_message_relay(event, match):
    if event.chan not in CHANNELS:
        log.warning(f"Ignoring channel {event.chan} for relay because not in {CHANNELS}")
        return
    send_buffer.append(serialize("message", event))

@hook.periodic(QUEUE_SEND_SECONDS)
def irc_publish(bot):
    global send_buffer
    bodies = [
        body for idx, body in enumerate(send_buffer) if idx+1 <= SEND_LIMIT
    ]
    if bodies:
        publish(bodies)
        send_buffer = send_buffer[len(bodies):]

@hook.event([
    EventType.join, 
    EventType.part,
    EventType.kick, 
    EventType.other,
    EventType.notice
])
def irc_event_relay(event):
    if event.chan not in CHANNELS:
        return

    lookup = {
        EventType.join: "join",
        EventType.part: "part",
        EventType.kick: "kick",
        # EventType.action: "action", # on TODO: no clue how to deal with this right now
        EventType.other: "other",
        EventType.notice: "notice"
    }

    if lookup[event.type] in ["join", "part"] and not SEND_JOIN_PART_EVENTS:
        return

    send_buffer.append(serialize(lookup[event.type], event))

@hook.irc_raw("QUIT")
def irc_quit_event_relay(event):
    if not SEND_JOIN_PART_EVENTS:
        return

    send_buffer.append(serialize("quit", event))

@hook.periodic(QUEUE_CHECK_SECONDS)
async def discord_receiver(bot):
    responses = consume()
    for response in responses:
        await handle_event(bot, response)

def _get_channel(data):
    for channel_name in CHANNELS:
        if channel_name == CHANNEL_MAP.get(data.channel.id):
            return channel_name

async def handle_event(bot, response):
    data = deserialize(response)
    if not data:
        log.warning("Unable to deserialize data! Aborting!")
        return

    event_type = data.event.type

    message = None
    if event_type == "message":
        message = format_message_event(data)
    elif event_type == "message_edit":
        message = format_message_edit_event(data)
    elif event_type == "reaction_add":
        message = format_reaction_add_event(data)

    if not message:
        log.warning(f"Unable to format message for event: {response}")
        return

    channel = _get_channel(data)
    if not channel:
        log.warning("Unable to find channel to send message")
        return

    try:
        bot.connections.get(IRC_CONNECTION).message(channel, message)
    except Exception as e:
        log.warning(f"Unable to send message to {channel}: {e}")
