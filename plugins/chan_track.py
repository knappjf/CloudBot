"""
Track channel ops for permissions checks

Requires:
server_info.py
"""
import gc
import json
from collections import Mapping, Iterable, namedtuple
from contextlib import suppress
from numbers import Number
from operator import attrgetter
from weakref import WeakValueDictionary

import cloudbot.bot
from cloudbot import hook
from cloudbot.util import web
from cloudbot.util.parsers.irc import Prefix

logger = cloudbot.bot.logger


class WeakDict(dict):
    # Subclass dict to allow it to be weakly referenced
    pass


# noinspection PyUnresolvedReferences
class KeyFoldMixin:
    def __contains__(self, item):
        return super().__contains__(item.casefold())

    def __getitem__(self, item):
        return super().__getitem__(item.casefold())

    def __setitem__(self, key, value):
        return super().__setitem__(key.casefold(), value)

    def __delitem__(self, key):
        return super().__delitem__(key.casefold())

    def pop(self, key, *args, **kwargs):
        return super().pop(key.casefold(), *args, **kwargs)

    def get(self, key, default=None):
        return super().get(key.casefold(), default)

    def setdefault(self, key, default=None):
        return super().setdefault(key.casefold(), default)

    def update(self, mapping=None, **kwargs):
        if mapping is not None:
            if hasattr(mapping, 'keys'):
                for k in mapping.keys():
                    self[k] = mapping[k]
            else:
                for k, v in mapping:
                    self[k] = v

        for k in kwargs:
            self[k] = kwargs[k]


class KeyFoldDict(KeyFoldMixin, dict):
    pass


class KeyFoldWeakValueDict(KeyFoldMixin, WeakValueDictionary):
    pass


class ChanDict(KeyFoldDict):
    def getchan(self, name):
        try:
            return self[name]
        except KeyError:
            self[name] = value = Channel(name)
            return value


class UsersDict(KeyFoldWeakValueDict):
    def getuser(self, nick):
        try:
            return self[nick]
        except KeyError:
            self[nick] = value = User(nick)
            return value


class MappingAttributeAdapter:
    def __init__(self):
        self.data = {}

    def __getitem__(self, item):
        try:
            return getattr(self, item)
        except AttributeError:
            return self.data[item]

    def __setitem__(self, key, value):
        if not hasattr(self, key):
            self.data[key] = value
        else:
            setattr(self, key, value)


class Channel(MappingAttributeAdapter):
    class Member(MappingAttributeAdapter):
        def __init__(self, user, channel):
            self.user = user
            self.channel = channel
            self.status = []
            super().__init__()

    def __init__(self, name):
        self.name = name
        self.users = KeyFoldDict()
        self.receiving_names = False
        super().__init__()

    def get_member(self, user, create=False):
        """
        :type user: User
        :type create: bool
        :rtype: Channel.Member
        """
        try:
            data = self.users[user.nick]
        except KeyError:
            if not create:
                raise

            self.users[user.nick] = data = self.Member(user, self)

        return data


class User(MappingAttributeAdapter):
    def __init__(self, name):
        self.mask = Prefix(name)
        self.realname = None
        self._account = None
        self.server = None

        self.is_away = False
        self.away_message = None

        self.is_oper = False

        self.channels = KeyFoldWeakValueDict()
        super().__init__()

    def join_channel(self, channel):
        """
        :type channel: Channel
        """
        self.channels[channel.name] = memb = channel.get_member(self, create=True)
        return memb

    @property
    def account(self):
        return self._account

    @account.setter
    def account(self, value):
        if value == '*':
            value = None

        self._account = value

    @property
    def nick(self):
        return self.mask.nick

    @nick.setter
    def nick(self, value):
        self.mask.nick = value

    @property
    def ident(self):
        return self.mask.user

    @ident.setter
    def ident(self, value):
        self.mask.user = value

    @property
    def host(self):
        return self.mask.host

    @host.setter
    def host(self, value):
        self.mask.host = value


# region util functions


def get_users(conn):
    """
    :type conn: cloudbot.client.Client
    :rtype: UsersDict
    """
    return conn.memory.setdefault("users", UsersDict())


def get_chans(conn):
    """
    :type conn: cloudbot.client.Client
    :rtype: ChanDict
    """
    return conn.memory.setdefault("chan_data", ChanDict())


# endregion util functions


def update_chan_data(conn, chan):
    """
    :type conn: cloudbot.client.Client
    :type chan: str
    """
    chan_data = get_chans(conn).getchan(chan)
    chan_data.receiving_names = False
    conn.cmd("NAMES", chan)


def update_conn_data(conn):
    """
    :type conn: cloudbot.client.Client
    """
    for chan in set(conn.channels):
        update_chan_data(conn, chan)


SUPPORTED_CAPS = frozenset({
    "userhost-in-names",
    "multi-prefix",
    "extended-join",
    "account-notify",
    "away-notify",
    "chghost",
})


@hook.on_cap_available(*SUPPORTED_CAPS)
def do_caps():
    return True


def is_cap_available(conn, cap):
    """
    :type conn: cloudbot.client.Client
    :type cap: str
    """
    caps = conn.memory.get("server_caps", {})
    return bool(caps.get(cap, False))


@hook.on_start
def get_chan_data(bot):
    """
    :type bot: cloudbot.bot.CloudBot
    """
    for conn in bot.connections.values():
        if conn.connected:
            init_chan_data(conn, False)
            update_conn_data(conn)


def sort_member_status(member):
    """
    :type member: Channel.Member
    """
    status = list(set(member.status))
    status.sort(key=attrgetter("level"), reverse=True)
    member.status = status


def clean_user_data(user):
    """
    :type user: User
    """
    for memb in user.channels.values():
        status = list(set(memb.status))
        status.sort(key=attrgetter("level"), reverse=True)
        memb.status = status


def clean_chan_data(chan):
    """
    :type chan: Channel
    """
    with suppress(KeyError):
        del chan.data["new_users"]


def clean_conn_data(conn):
    """
    :type conn: cloudbot.client.Client
    """
    for user in get_users(conn).values():
        clean_user_data(user)

    for chan in get_chans(conn).values():
        clean_chan_data(chan)


def clean_data(bot):
    """
    :type bot: cloudbot.bot.CloudBot
    """
    for conn in bot.connections.values():
        clean_conn_data(conn)


@hook.connect
def init_chan_data(conn, _clear=True):
    """
    :type conn: cloudbot.client.Client
    :type _clear: bool
    """
    chan_data = get_chans(conn)
    users = get_users(conn)

    if not (isinstance(chan_data, ChanDict) and isinstance(users, UsersDict)):
        del conn.memory["chan_data"]
        del conn.memory["users"]

        return init_chan_data(conn, _clear)

    if _clear:
        chan_data.clear()
        users.clear()


def parse_names_item(item, statuses, has_multi_prefix, has_userhost):
    user_status = []
    while item[:1] in statuses:
        status, item = item[:1], item[1:]
        user_status.append(statuses[status])
        if not has_multi_prefix:
            # Only remove one status prefix if we don't have multi prefix enabled
            break

    user_status.sort(key=attrgetter('level'), reverse=True)

    if has_userhost:
        prefix = Prefix.parse(item)
    else:
        prefix = Prefix(item)

    return prefix.nick, prefix.user, prefix.host, user_status


def replace_user_data(conn, chan_data):
    """
    :type conn: cloudbot.client.Client
    :type chan_data: Channel
    """
    statuses = {status.prefix: status for status in set(conn.memory["server_info"]["statuses"].values())}
    new_data = chan_data.data.pop("new_users", [])
    new_users = KeyFoldDict()
    has_uh_i_n = is_cap_available(conn, "userhost-in-names")
    has_multi_pfx = is_cap_available(conn, "multi-prefix")
    for name in new_data:
        nick, ident, host, status = parse_names_item(name, statuses, has_multi_pfx, has_uh_i_n)
        user_data = get_users(conn).getuser(nick)
        user_data.mask = Prefix(nick, ident, host)

        new_users[nick] = memb_data = user_data.join_channel(chan_data)
        memb_data.status = status

    old_users = chan_data.users
    old_users.clear()
    old_users.update(new_users)  # Reassigning the dict would break other references to the data, so just update instead


@hook.irc_raw(['353', '366'], singlethread=True)
def on_names(conn, irc_paramlist, irc_command):
    """
    :type conn: cloudbot.client.Client
    :type irc_paramlist: cloudbot.util.parsers.irc.ParamList
    :type irc_command: str
    """
    chan = irc_paramlist[2 if irc_command == '353' else 1]
    chan_data = get_chans(conn).getchan(chan)
    if irc_command == '366':
        chan_data.receiving_names = False
        replace_user_data(conn, chan_data)
        return

    users = chan_data.data.setdefault("new_users", [])
    if not chan_data.receiving_names:
        chan_data.receiving_names = True
        users.clear()

    names = irc_paramlist[-1]
    if names.startswith(':'):
        names = names[1:].strip()

    users.extend(names.split())


class MappingSerializer:
    def __init__(self):
        self._seen_objects = []

    def _serialize(self, obj):
        if isinstance(obj, (str, Number, bool)) or obj is None:
            return obj
        elif isinstance(obj, Mapping):
            if id(obj) in self._seen_objects:
                return '<{} with id {}>'.format(type(obj).__name__, id(obj))

            self._seen_objects.append(id(obj))

            return {
                self._serialize(k): self._serialize(v)
                for k, v in obj.items()
            }
        elif isinstance(obj, Iterable):
            if id(obj) in self._seen_objects:
                return '<{} with id {}>'.format(type(obj).__name__, id(obj))

            self._seen_objects.append(id(obj))

            return [
                self._serialize(item)
                for item in obj
            ]
        else:
            return repr(obj)

    def serialize(self, mapping, **kwargs):
        return json.dumps(self._serialize(mapping), **kwargs)


@hook.permission("chanop")
def perm_check(chan, conn, nick):
    """
    :type chan: str
    :type conn: cloudbot.client.Client
    :type nick: str
    """
    if not (chan and conn):
        return False

    chans = get_chans(conn)
    try:
        chan_data = chans[chan]
    except KeyError:
        return False

    try:
        memb = chan_data.users[nick]
    except KeyError:
        return False

    status = memb.status
    if status and status[0].level > 1:
        return True

    return False


@hook.command(permissions=["botcontrol"], autohelp=False)
def dumpchans(conn):
    """- Dumps all stored channel data for this connection to the console
    :type conn: cloudbot.client.Client
    """
    data = get_chans(conn)
    return web.paste(MappingSerializer().serialize(data, indent=2))


@hook.command(permissions=["botcontrol"], autohelp=False)
def dumpusers(conn):
    """- Dumps all stored user data for this connection to the console
    :type conn: cloudbot.client.Client
    """
    data = get_users(conn)
    return web.paste(MappingSerializer().serialize(data, indent=2))


@hook.command(permissions=["botcontrol"], autohelp=False)
def updateusers(bot):
    """- Forces an update of all /NAMES data for all channels
    :type bot: cloudbot.bot.CloudBot
    """
    get_chan_data(bot)
    return "Updating all channel data"


@hook.command(permissions=["botcontrol"], autohelp=False)
def cleanusers(bot):
    """
    :type bot: cloudbot.bot.CloudBot
    """
    clean_data(bot)
    gc.collect()
    return "Data cleaned."


@hook.command(permissions=["botcontrol"], autohelp=False)
def clearusers(bot):
    """
    :type bot: cloudbot.bot.CloudBot
    """
    for conn in bot.connections.values():
        init_chan_data(conn, True)

    gc.collect()
    return "Data cleared."


@hook.command("getdata", permissions=["botcontrol"], autohelp=False)
def getdata_cmd(conn, chan, text, nick):
    chan_data = get_chans(conn).getchan(chan)
    user_data = get_users(conn).getuser(nick)
    memb = chan_data.get_member(user_data)
    return web.paste(MappingSerializer().serialize(memb, indent=2))


@hook.irc_raw('JOIN')
def on_join(nick, user, host, conn, irc_paramlist):
    """
    :type nick: str
    :type user: str
    :type host: str
    :type conn: cloudbot.client.Client
    :type irc_paramlist: cloudbot.util.parsers.irc.ParamList
    """
    chan, *other_data = irc_paramlist

    if chan.startswith(':'):
        chan = chan[1:]

    users = get_users(conn)

    user_data = users.getuser(nick)

    user_data.ident = user
    user_data.host = host

    if is_cap_available(conn, "extended-join") and other_data:
        acct, realname = other_data
        user_data.account = acct
        user_data.realname = realname

    chan_data = get_chans(conn).getchan(chan)
    user_data.join_channel(chan_data)


ModeChange = namedtuple('ModeChange', 'mode adding param is_status')


def _parse_mode_string(modes, params, status_modes, mode_types):
    new_modes = []
    adding = True
    for c in modes:
        if c == '+':
            adding = True
        elif c == '-':
            adding = False
        else:
            is_status = c in status_modes
            mode_type = mode_types.get(c)
            if mode_type:
                mode_type = mode_type.type
            else:
                mode_type = 'B' if is_status else None

            if mode_type in "AB" or (mode_type == 'C' and adding):
                param = params.pop(0)
            else:
                param = None

            new_modes.append(ModeChange(c, adding, param, is_status))

    return new_modes


@hook.irc_raw('MODE')
def on_mode(chan, irc_paramlist, conn):
    """
    :type chan: str
    :type conn: cloudbot.client.Client
    :type irc_paramlist: cloudbot.util.parsers.irc.ParamList
    """
    if chan.startswith(':'):
        chan = chan[1:]

    if chan.casefold() == conn.nick.casefold():
        # this is a user mode line
        return

    serv_info = conn.memory["server_info"]
    statuses = serv_info["statuses"]
    status_modes = {status.mode for status in statuses.values()}
    mode_types = serv_info["channel_modes"]

    chan_data = get_chans(conn).getchan(chan)

    modes = irc_paramlist[1]
    mode_params = list(irc_paramlist[2:]).copy()
    new_modes = _parse_mode_string(modes, mode_params, status_modes, mode_types)
    new_statuses = [change for change in new_modes if change.is_status]
    to_sort = {}
    for change in new_statuses:
        status_char = change.mode
        nick = change.param
        user = get_users(conn).getuser(nick)
        memb = chan_data.get_member(user, create=True)
        status = statuses[status_char]
        memb_status = memb.status
        if change.adding:
            memb_status.append(status)
            to_sort[user.nick] = memb
        else:
            if status in memb_status:
                memb_status.remove(status)
            else:
                logger.debug(
                    "[%s|chantrack] Attempt to remove status %s from user %s in channel %s",
                    conn.name, status, user['nick'], chan
                )

    for member in to_sort.values():
        sort_member_status(member)


@hook.irc_raw('PART')
def on_part(chan, nick, conn):
    """
    :type chan: str
    :type nick: str
    :type conn: cloudbot.client.Client
    """
    if chan.startswith(':'):
        chan = chan[1:]

    channels = get_chans(conn)
    if nick.casefold() == conn.nick.casefold():
        del channels[chan]
    else:
        chan_data = channels[chan]
        del chan_data.users[nick]


@hook.irc_raw('KICK')
def on_kick(chan, target, conn):
    """
    :type chan: str
    :type target: str
    :type conn: cloudbot.client.Client
    """
    on_part(chan, target, conn)


@hook.irc_raw('QUIT')
def on_quit(nick, conn):
    """
    :type nick: str
    :type conn: cloudbot.client.Client
    """
    users = get_users(conn)
    if nick in users:
        user = users.pop(nick)
        for memb in user.channels.values():
            chan = memb.channel
            del chan.users[nick]


@hook.irc_raw('NICK')
def on_nick(nick, irc_paramlist, conn):
    """
    :type nick: str
    :type irc_paramlist: cloudbot.util.parsers.irc.ParamList
    :type conn: cloudbot.client.Client
    """
    users = get_users(conn)
    new_nick = irc_paramlist[0]
    if new_nick.startswith(':'):
        new_nick = new_nick[1:]

    user = users.pop(nick)
    users[new_nick] = user
    user.nick = new_nick
    for memb in user.channels.values():
        chan_users = memb.channel.users
        chan_users[new_nick] = chan_users.pop(nick)


@hook.irc_raw('ACCOUNT')
def on_account(conn, nick, irc_paramlist):
    """
    :type nick: str
    :type irc_paramlist: cloudbot.util.parsers.irc.ParamList
    :type conn: cloudbot.client.Client
    """
    get_users(conn).getuser(nick).account = irc_paramlist[0]


@hook.irc_raw('CHGHOST')
def on_chghost(conn, nick, irc_paramlist):
    """
    :type nick: str
    :type irc_paramlist: cloudbot.util.parsers.irc.ParamList
    :type conn: cloudbot.client.Client
    """
    ident, host = irc_paramlist
    user = get_users(conn).getuser(nick)
    user.ident = ident
    user.host = host


@hook.irc_raw('AWAY')
def on_away(conn, nick, irc_paramlist):
    """
    :type nick: str
    :type irc_paramlist: cloudbot.util.parsers.irc.ParamList
    :type conn: cloudbot.client.Client
    """
    if irc_paramlist:
        reason = irc_paramlist[0]
    else:
        reason = None

    user = get_users(conn).getuser(nick)
    user.is_away = (reason is not None)
    user.away_message = reason


@hook.irc_raw('352')
def on_who(conn, irc_paramlist):
    """
    :type irc_paramlist: cloudbot.util.parsers.irc.ParamList
    :type conn: cloudbot.client.Client
    """
    _, _, ident, host, server, nick, status, realname = irc_paramlist
    realname = realname.split(None, 1)[1]
    user = get_users(conn).getuser(nick)
    status = list(status)
    is_away = status.pop(0) == "G"
    is_oper = status[:1] == "*"
    user.ident = ident
    user.host = host
    user.server = server
    user.realname = realname
    user.is_away = is_away
    user.is_oper = is_oper


@hook.irc_raw('311')
def on_whois_name(conn, irc_paramlist):
    """
    :type irc_paramlist: cloudbot.util.parsers.irc.ParamList
    :type conn: cloudbot.client.Client
    """
    _, nick, ident, host, _, realname = irc_paramlist
    user = get_users(conn).getuser(nick)
    user.ident = ident
    user.host = host
    user.realname = realname


@hook.irc_raw('330')
def on_whois_acct(conn, irc_paramlist):
    """
    :type irc_paramlist: cloudbot.util.parsers.irc.ParamList
    :type conn: cloudbot.client.Client
    """
    _, nick, acct = irc_paramlist[:2]
    get_users(conn).getuser(nick).account = acct


@hook.irc_raw('301')
def on_whois_away(conn, irc_paramlist):
    """
    :type irc_paramlist: cloudbot.util.parsers.irc.ParamList
    :type conn: cloudbot.client.Client
    """
    _, nick, msg = irc_paramlist
    user = get_users(conn).getuser(nick)
    user.is_away = True
    user.away_message = msg


@hook.irc_raw('312')
def on_whois_server(conn, irc_paramlist):
    """
    :type irc_paramlist: cloudbot.util.parsers.irc.ParamList
    :type conn: cloudbot.client.Client
    """
    _, nick, server, _ = irc_paramlist
    get_users(conn).getuser(nick).server = server


@hook.irc_raw('313')
def on_whois_oper(conn, irc_paramlist):
    """
    :type irc_paramlist: cloudbot.util.parsers.irc.ParamList
    :type conn: cloudbot.client.Client
    """
    nick = irc_paramlist[1]
    get_users(conn).getuser(nick).is_oper = True
