import logging
import datetime
from inspect import isclass
from logging import getLevelName, addLevelName
from os import getpid, PathLike
from os.path import normcase, basename, splitext
import sys
from sys import exc_info, stdout as STDOUT, stderr as STDERR
from multiprocessing import current_process
from threading import current_thread
import traceback
from numbers import Number
import shutil
import re
import os
import glob
from collections import defaultdict, OrderedDict
from string import Formatter
import math
import functools
import uuid
import importlib

import ansimarkup
from better_exceptions_fork import ExceptionFormatter
from pendulum import now
import pendulum


NOTSET = 0
TRACE = 5
DEBUG = 10
INFO = 20
SUCCESS = 25
WARNING = 30
ERROR = 40
CRITICAL = 50

addLevelName(TRACE, "TRACE")
addLevelName(SUCCESS, "SUCCESS")

LEVELS_COLORS = {
    getLevelName(TRACE): "<cyan><bold>",
    getLevelName(DEBUG): "<blue><bold>",
    getLevelName(INFO): "<bold>",
    getLevelName(SUCCESS): "<green><bold>",
    getLevelName(WARNING): "<yellow><bold>",
    getLevelName(ERROR): "<red><bold>",
    getLevelName(CRITICAL): "<RED><bold>",
}

VERBOSE_FORMAT = "<green>{time}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"

DAYS_NAMES = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY', 'SUNDAY']

__version__ = "0.0.1"

start_time = now()

def getframe_fallback(n):
    """Return the frame object for the caller's stack frame."""
    try:
        raise Exception
    except Exception:
        frame = exc_info()[2].tb_frame.f_back
        for _ in range(n):
            frame = frame.f_back
        return frame

def get_getframe_function():
    if hasattr(sys, '_getframe'):
        getframe = sys._getframe
    else:
        getframe = getframe_fallback
    return getframe

getframe = get_getframe_function()

def patch_datetime(date):
    date._FORMATTER = 'alternative'

def patch_datetime_file(date):
    date._FORMATTER = 'alternative'
    date._to_string_format = '%Y-%m-%d_%H-%M-%S'

class loguru_traceback:
    __slots__ = ('tb_frame', 'tb_lasti', 'tb_lineno', 'tb_next', '__is_caught_point__')

    def __init__(self, frame, lasti, lineno, next_=None, is_caught_point=False):
        self.tb_frame = frame
        self.tb_lasti = lasti
        self.tb_lineno = lineno
        self.tb_next = next_
        self.__is_caught_point__ = is_caught_point


class StrRecord(str):
    pass

class HackyInt(int):

    rand = str(uuid.uuid4().int) + str(uuid.uuid4().int)  # 32 bytes

    def __str__(self):
        self.true_value = int(repr(self))
        self.false_value = '0' + repr(self) + self.rand
        return self.false_value

    def __eq__(self, other):
        return False

class Handler:

    def __init__(self, *, writter, level, format_, filter_, colored, better_exceptions):
        self.writter = writter
        self.level = level
        self.format = format_
        self.filter = filter_
        self.colored = colored
        self.better_exceptions = better_exceptions

        self.formats_per_level = self.generate_formats(format_, colored)
        self.exception_formatter = ExceptionFormatter(colored=colored)

    @staticmethod
    def generate_formats(format_, colored):
        formats_per_level = {}

        for level_name, level_color in LEVELS_COLORS.items():
            color = ansimarkup.parse(level_color)
            custom_markup = dict(level=color, lvl=color)
            am = ansimarkup.AnsiMarkup(tags=custom_markup)
            formats_per_level[level_name] = am.parse(format_) if colored else am.strip(format_)

        return formats_per_level

    def emit(self, record):
        level = record['level']
        if self.level > level.no:
            return

        if self.filter is not None:
            if not self.filter(record):
                return

        exception = record['exception']

        formatted = self.formats_per_level[level.name].format_map(record) + '\n'

        if exception:
            hacked = None
            tb = exception[2]
            while tb:
                if tb.__is_caught_point__:
                    hacked = HackyInt(tb.tb_lineno)
                    tb.tb_lineno = hacked
                    break
                tb = tb.tb_next

            if self.better_exceptions:
                formatted_exception = self.exception_formatter.format_exception(*exception)
            else:
                formatted_exception = traceback.format_exception(*exception)

            formatted_exception = ''.join(formatted_exception)

            tb_reg = r'Traceback \(most recent call last\):'
            ansi_reg = r'[a-zA-Z0-9;\\\[]*'
            hacky_reg = r'^({ansi})({tb})({ansi})$((?:(?!^{ansi}{tb}{ansi}$)[\s\S])*)^({ansi})(  )({ansi}File.*line{ansi} {ansi})({line})({ansi},.*)$'.format(tb=tb_reg, ansi=ansi_reg, line=str(hacked.false_value))

            def mark_catch_point(match):
                m_1, tb, m_2, m_3, m_4, s, m_5, line, m_6 = match.groups()
                tb = 'Traceback (most recent call last, catch point marked):'
                s = '> '
                line = str(hacked.true_value)
                return ''.join([m_1, tb, m_2, m_3, m_4, s, m_5, line, m_6])

            formatted_exception = re.sub(hacky_reg, mark_catch_point, formatted_exception, count=1, flags=re.M)

            formatted += formatted_exception


        message = StrRecord(formatted)
        message.record = record

        self.writter(message)

class LevelRecattr(str):
    __slots__ = ('no', 'name')


class FileRecattr(str):
    __slots__ = ('name', 'path')


class ThreadRecattr(str):
    __slots__ = ('id', 'name')


class ProcessRecattr(str):
    __slots__ = ('id', 'name')


class FileSink:

    def __init__(self, path, *, rotation=None, backups=None, compression=None, **kwargs):
        self.start_time = now()
        patch_datetime_file(self.start_time)
        self.kwargs = kwargs.copy()
        self.kwargs.setdefault('mode', 'a')
        self.kwargs.setdefault('buffering', 1)
        self.path = str(path)
        self.file = None
        self.file_path = None
        self.created = 0
        self.rotation_time = None

        self.should_rotate = self.make_should_rotate_function(rotation)
        self.manage_backups = self.make_manage_backups_function(backups)
        self.compress_file = self.make_compress_file_function(compression)
        self.regex_file_name = self.make_regex_file_name(os.path.basename(self.path))

        self.rotate()

        if self.should_rotate is None:
            self.write = self.file.write
        else:
            self.write = self.rotating_write

    def format_path(self):
        now_ = now()
        patch_datetime_file(now_)

        record = {
            "time": now_,
            "start_time": self.start_time,
            "rotation_time": self.rotation_time,
            "n": self.created,
            "n+1": self.created + 1,
        }

        return self.path.format_map(record)

    @staticmethod
    def make_regex_file_name(file_name):
        tokens = Formatter().parse(file_name)
        regex_name = ''.join(re.escape(t[0]) + '.*' * (t[1] is not None) for t in tokens)
        regex_name += '(?:\.\d+)?'
        regex_name += '(?:\.(?:gz(?:ip)?|bz(?:ip)?2|xz|lzma|zip))?'
        return re.compile(regex_name)

    def make_should_rotate_function(self, rotation):
        if rotation is None:
            return None
        elif isinstance(rotation, str):
            size = self.parse_size(rotation)
            if size is not None:
                return self.make_should_rotate_function(size)
            interval = self.parse_duration(rotation)
            if interval is not None:
                return self.make_should_rotate_function(interval)
            frequency = self.parse_frequency(rotation)
            if frequency is not None:
                return self.make_should_rotate_function(frequency)
            daytime = self.parse_daytime(rotation)
            if daytime is not None:
                day, time = daytime
                if day is None:
                    return self.make_should_rotate_function(time)
                elif time is None:
                    time = pendulum.parse('00:00', strict=True)
                day = getattr(pendulum, DAYS_NAMES[day])
                time_limit = self.start_time.at(time.hour, time.minute, time.second, time.microsecond)
                if time_limit <= self.start_time:
                    time_limit = time_limit.next(day, keep_time=True)
                self.rotation_time = time_limit
                def function(message):
                    nonlocal time_limit
                    record_time = message.record['time']
                    if record_time >= time_limit:
                        while time_limit <= record_time:
                            time_limit = time_limit.next(day, keep_time=True)
                        self.rotation_time = time_limit
                        return True
                    return False
            else:
                raise ValueError("Cannot parse rotation from: '%s'" % rotation)
        elif isinstance(rotation, Number):
            size_limit = rotation
            def function(message):
                file = self.file
                file.seek(0, 2)
                return file.tell() + len(message) >= size_limit
        elif isinstance(rotation, datetime.time):
            time = pendulum.Time.instance(rotation)
            time_limit = self.start_time.at(time.hour, time.minute, time.second, time.microsecond)
            if time_limit <= self.start_time:
                time_limit.add(days=1)
            self.rotation_time = time_limit
            def function(message):
                nonlocal time_limit
                record_time = message.record['time']
                if record_time >= time_limit:
                    while time_limit <= record_time:
                        time_limit = time_limit.add(days=1)
                    self.rotation_time = time_limit
                    return True
                return False
        elif isinstance(rotation, datetime.timedelta):
            time_delta = pendulum.Interval.instance(rotation)
            time_limit = self.start_time + time_delta
            self.rotation_time = time_limit
            def function(message):
                nonlocal time_limit
                record_time = message.record['time']
                if record_time >= time_limit:
                    while time_limit <= record_time:
                        time_limit += time_delta
                    self.rotation_time = time_limit
                    return True
                return False
        elif callable(rotation):
            time_limit = rotation(self.start_time)
            def function(message):
                nonlocal time_limit
                record_time = message.record['time']
                if record_time >= time_limit:
                    time_limit = rotation(record_time)
                    self.rotation_time = time_limit
                    return True
                return False
        else:
            raise ValueError("Cannot infer rotation for objects of type: '%s'" % type(rotation))

        return function

    def make_manage_backups_function(self, backups):
        if backups is None:
            return None
        elif isinstance(backups, str):
            interval = self.parse_duration(backups)
            if interval is None:
                raise ValueError("Cannot parse backups from: '%s'" % backups)
            return self.make_manage_backups_function(interval)
        elif isinstance(backups, int):
            def function(logs):
                return sorted(logs, key=lambda log: (-log.stat().st_mtime, log.name))[backups:]
        elif isinstance(backups, datetime.timedelta):
            seconds = backups.total_seconds()
            def function(logs):
                t = now().timestamp()
                limit = t - seconds
                return [log for log in logs if log.stat().st_mtime <= limit]
        elif callable(backups):
            function = backups
        else:
            raise ValueError("Cannot infer backups for objects of type: '%s'" % type(backups))

        return function

    def make_compress_file_function(self, compression):
        if compression is None or compression is False:
            return None
        elif compression is True:
            return self.make_compress_file_function('gz')
        elif isinstance(compression, str):
            compress_format = compression.strip().lstrip('.')
            compress_format_lower = compress_format.lower()

            compress_module = None
            compress_args = {}
            compress_func = shutil.copyfileobj

            if compress_format_lower in ['gz', 'gzip']:
                import gzip
                compress_module = gzip
            elif compress_format_lower in ['bz2', 'bzip2']:
                import bz2
                compress_module = bz2
            elif compress_format_lower == 'xz':
                import lzma
                compress_module = lzma
                compress_args = dict(format=lzma.FORMAT_ALONE)
            elif compress_format_lower == 'lzma':
                import lzma
                compress_module = lzma
                compress_args = dict(format=lzma.FORMAT_XZ)
            elif compress_format_lower == 'zip':
                import zlib  # Used by zipfile, so check it's available
                import zipfile
                def func(path):
                    compress_path = '%s.%s' % (path, compress_format)
                    with zipfile.ZipFile(compress_path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
                        z.write(path)
                    os.remove(path)
                return func
            else:
                raise ValueError("Invalid compression format: '%s'" % compress_format)

            def func(path):
                with open(path, 'rb') as f_in:
                    compress_path = '%s.%s' % (path, compress_format)
                    with compress_module.open(compress_path, 'wb', **compress_args) as f_out:
                        compress_func(f_in, f_out)
                os.remove(path)

            return func

        elif callable(compression):
            return compression
        else:
            raise ValueError("Cannot infer compression for objects of type: '%s'" % type(compression))

    @staticmethod
    def parse_size(size):
        size = size.strip()
        reg = r'([e\+\-\.\d]+)\s*([kmgtpezy])?(i)?(b)'
        match = re.fullmatch(reg, size, flags=re.I)
        if not match:
            return None
        s, u, i, b = match.groups()
        try:
            s = float(s)
        except ValueError:
            raise ValueError("Invalid float value while parsing size: '%s'" % s)
        u = 'kmgtpezy'.index(u.lower()) + 1 if u else 0
        i = 1024 if i else 1000
        b = {'b': 8, 'B': 1}[b] if b else 1
        size = s * i**u / b

        return size

    @staticmethod
    def parse_duration(duration):
        duration = duration.strip()

        units = [
            ('y|years?', 31536000),
            ('mo|months?', 2628000),
            ('w|weeks?', 604800),
            ('d|days?', 86400),
            ('h|hours?', 3600),
            ('m|minutes?', 60),
            ('s|seconds?', 1),
            ('ms|milliseconds?', 0.001),
            ('us|microseconds?', 0.000001),
        ]

        reg = r'(?:([e\+\-\.\d]+)\s*([a-z]+)[\s\,]*)'
        if not re.fullmatch(reg + '+', duration, flags=re.I):
            return None

        seconds = 0

        for value, unit in re.findall(reg, duration, flags=re.I):
            try:
                value = float(value)
            except ValueError:
                raise ValueError("Invalid float value while parsing duration: '%s'" % value)

            try:
                unit = next(u for r, u in units if re.fullmatch(r, unit, flags=re.I))
            except StopIteration:
                raise ValueError("Invalid unit value while parsing duration: '%s'" % unit)

            seconds += value * unit

        return pendulum.Interval(seconds=seconds)

    @staticmethod
    def parse_frequency(frequency):
        frequency = frequency.strip().lower()
        function = None

        if frequency == 'hourly':
            function = lambda t: t.add(hours=1).start_of('hour')
        elif frequency == 'daily':
            function = '00:00'
        elif frequency == 'weekly':
            function = 'w0'
        elif frequency == 'monthly':
            function = lambda t: t.add(months=1).start_of('month')
        elif frequency == 'yearly':
            function = lambda t: t.add(years=1).start_of('year')

        return function

    @staticmethod
    def parse_daytime(daytime):
        daytime = daytime.strip()

        daytime_reg = re.compile(r'(.*?)\s+at\s+(.*)', flags=re.I)
        day_reg = re.compile(r'w\d+', flags=re.I)
        time_reg = re.compile(r'[\d\.\:\,]+(?:\s*[ap]m)?', flags=re.I)

        daytime_match = daytime_reg.fullmatch(daytime)
        if daytime_match:
            day, time = daytime_match.groups()
        elif time_reg.fullmatch(daytime):
            day, time = None, daytime
        elif day_reg.fullmatch(daytime) or daytime.upper() in DAYS_NAMES:
            day, time = daytime, None
        else:
            return None

        if day is not None:
            if day_reg.fullmatch(day):
                day = int(day[1:])
                if not 0 <= day <= 6:
                    raise ValueError("Invalid weekday index while parsing daytime: '%d'" % day)
            elif day.upper() in DAYS_NAMES:
                day = DAYS_NAMES.index(day.upper())
            else:
                raise ValueError("Invalid weekday value while parsing daytime: '%s'" % day)

        if time is not None:
            time_ = time
            try:
                time = pendulum.parse(time, strict=True)
            except Exception as e:
                raise ValueError("Invalid time while parsing daytime: '%s'" % time) from e
            else:
                if not isinstance(time, datetime.time):
                    raise ValueError("Cannot strictly parse time from: '%s'" % time_)

        return day, time

    def rotating_write(self, message):
        if self.should_rotate(message):
            self.rotate()
        self.file.write(message)

    def rotate(self):
        old_path = self.file_path
        self.stop()
        file_path = os.path.abspath(self.format_path())
        file_dir = os.path.dirname(file_path)

        os.makedirs(file_dir, exist_ok=True)

        if self.manage_backups is not None:
            regex_file_name = self.regex_file_name
            with os.scandir(file_dir) as it:
                logs = [f for f in it if regex_file_name.fullmatch(f.name) and f.is_file()]

            for log in self.manage_backups(logs):
                os.remove(log.path)

        if self.created > 0 and os.path.exists(file_path):
            basename = os.path.basename(file_path)
            reg = re.escape(basename) + '(?:\.(\d+))?(\.(?:gz(?:ip)?|bz(?:ip)?2|xz|lzma|zip))?'
            reg = re.compile(reg, flags=re.I)
            with os.scandir(file_dir) as it:
                logs = [f for f in it if f.is_file() and reg.fullmatch(f.name) and f.name != basename]
            logs.sort(key=lambda f: -int(reg.fullmatch(f.name).group(1) or 0))

            n = len(logs) + 1
            z = len(str(n))
            for i, log in enumerate(logs):
                os.replace(log.path, file_path + '.%s' % str(n - i).zfill(z) + (reg.fullmatch(log.name).group(2) or ''))
            new_path = file_path + ".%s" % "1".zfill(z)
            os.replace(file_path, new_path)

            if file_path == old_path:
                old_path = new_path

        if self.compress_file is not None and old_path is not None and os.path.exists(old_path):
            self.compress_file(old_path)

        self.file = open(file_path, **self.kwargs)
        self.file_path = file_path
        self.created += 1

    def stop(self):
        if self.file is not None:
            if self.compress_file is not None and self.should_rotate is None:
                self.compress_file(self.file_path)
            self.file.close()
            self.file = None
            self.file_path = None

class Catcher:

    def __init__(self, logger, exception=BaseException, *, level=None, reraise=False,
                       message="An error has been caught in function '{function}', "
                               "process '{process.name}' ({process.id}), "
                               "thread '{thread.name}' ({thread.id}):"):
        self.logger = logger
        self.exception = exception
        self.level = level
        self.reraise = reraise
        self.message = message

        self.function_name = None
        self.exception_logger = self.logger.exception

    def __enter__(self):
        pass

    def __exit__(self, type_, value, traceback_):
        if type_ is None:
            return

        if not issubclass(type_, self.exception):
            return False

        thread = current_thread()
        thread_recattr = ThreadRecattr(thread.ident)
        thread_recattr.id, thread_recattr.name = thread.ident, thread.name

        process = current_process()
        process_recattr = ProcessRecattr(process.ident)
        process_recattr.id, process_recattr.name = process.ident, process.name

        function_name = self.function_name
        if function_name is None:
            function_name = getframe(1).f_code.co_name

        record = {
            'process': process_recattr,
            'thread': thread_recattr,
            'function': function_name,
        }

        if self.level is not None:
            # TODO: Use logger function accordingly
            raise NotImplementedError

        self.exception_logger(self.message.format_map(record))

        return not self.reraise

    def __call__(self, *args, **kwargs):
        if not kwargs and len(args) == 1:
            arg = args[0]
            if callable(arg) and (not isclass(arg) or not issubclass(arg, BaseException)):
                function = arg
                function_name = function.__name__

                @functools.wraps(function)
                def catch_wrapper(*args, **kwargs):
                    # TODO: Fix it to avoid any conflict with threading because of self modification
                    self.function_name = function_name
                    self.exception_logger = self.logger._exception_catcher
                    with self:
                        function(*args, **kwargs)
                    self.function_name = None
                    self.exception_logger = self.logger.exception

                return catch_wrapper

        return Catcher(self.logger, *args, **kwargs)

class Logger:

    def __init__(self, *, dummy=None):
        self.dummy = dummy
        self.handlers_count = 0
        self.handlers = {}
        self.catch = Catcher(self)

    def log_to(self, sink, *, level=DEBUG, format=VERBOSE_FORMAT, filter=None, colored=None, better_exceptions=True, **kwargs):
        if isclass(sink):
            sink = sink(**kwargs)
            return self.log_to(sink, level=level, format=format, filter=filter, colored=colored, better_exceptions=better_exceptions)
        elif callable(sink):
            if kwargs:
                writter = lambda m: sink(m, **kwargs)
            else:
                writter = sink
            if colored is None:
                colored = False
        elif isinstance(sink, (str, PathLike)):
            path = sink
            sink = FileSink(path, **kwargs)
            return self.log_to(sink, level=level, format=format, filter=filter, colored=colored, better_exceptions=better_exceptions)
        elif hasattr(sink, 'write') and callable(sink.write):
            sink_write = sink.write
            if kwargs:
                write = lambda m: sink_write(m, **kwargs)
            else:
                write = sink_write

            if hasattr(sink, 'flush') and callable(sink.flush):
                sink_flush = sink.flush
                writter = lambda m: write(m) and sink_flush()
            else:
                writter = write

            if colored is None:
                try:
                    colored = sink.isatty()
                except Exception:
                    colored = False
        else:
            type_name = type(sink).__name__
            raise ValueError("Cannot log to objects of type '{}'.".format(type_name))

        if isinstance(filter, str):
            parent = filter + '.' * bool(filter)
            length = len(parent)
            filter = lambda r: (r['name'] + '.')[:length] == parent

        handler = Handler(
            writter=writter,
            level=level,
            format_=format,
            filter_=filter,
            colored=colored,
            better_exceptions=better_exceptions,
        )

        self.handlers[self.handlers_count] = (sink, handler)
        self.handlers_count += 1

        return self.handlers_count - 1

    def clear(self, handler_id=None):
        if handler_id is None:
            for sink, _ in self.handlers.values():
                if hasattr(sink, 'stop') and callable(sink.stop):
                    sink.stop()
            count = len(self.handlers)
            self.handlers.clear()
            return count
        elif handler_id in self.handlers:
            sink, _ = self.handlers.pop(handler_id)
            if hasattr(sink, 'stop') and callable(sink.stop):
                sink.stop()
            return 1
        return 0

    def config(self, source=None, *, sinks=None, dummy=None):
        if source is None:
            dict_config = {}
        elif isinstance(source, dict):
            dict_config = source
        elif isinstance(source, (str, PathLike)):
            source = str(source)
            name = 'loguru.dynamic_config_loader'
            loader = importlib.machinery.SourceFileLoader(name, source)
            spec = importlib.util.spec_from_loader(name, loader)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            dict_config = module.config
        else:
            raise ValueError("Cannot get dict config for objects of type: '%s'" % type(source))

        kwargs = {
            'sinks': sinks,
            'dummy': dummy,
        }

        for key, value in kwargs.items():
            if value is not None:
                dict_config[key] = value

        self.clear()
        self.dummy = dict_config.get('dummy', False)
        sinks_ids = [self.log_to(**params) for params in dict_config.get('sinks', [])]

        return sinks_ids

    @staticmethod
    def make_log_function(level, log_exception=0):

        level_name = getLevelName(level)

        def log_function(self, message, *args, **kwargs):
            frame = getframe(1)
            name = frame.f_globals['__name__']

            # TODO: Early exit if no handler

            now_ = now()
            patch_datetime(now_)

            message = message.format(*args, **kwargs)

            code = frame.f_code
            file_path = normcase(code.co_filename)
            file_name = basename(file_path)
            thread = current_thread()
            process = current_process()
            diff = now_ - start_time
            elapsed = pendulum.Interval(microseconds=diff.microseconds)

            level_recattr = LevelRecattr(level_name)
            level_recattr.no, level_recattr.name = level, level_name

            file_recattr = FileRecattr(file_name)
            file_recattr.name, file_recattr.path = file_name, file_path

            thread_recattr = ThreadRecattr(thread.ident)
            thread_recattr.id, thread_recattr.name = thread.ident, thread.name

            process_recattr = ProcessRecattr(process.ident)
            process_recattr.id, process_recattr.name = process.ident, process.name

            exception = None
            if log_exception:
                ex_type, ex, tb = exc_info()

                root_frame = tb.tb_frame.f_back

                # TODO: Test edge cases (look in CPython source code for traceback objects and exc.__traceback__ usages)

                loguru_tb = root_tb = None
                while tb:
                    if tb.tb_frame.f_code.co_filename != __file__:
                        new_tb = loguru_traceback(tb.tb_frame, tb.tb_lasti, tb.tb_lineno, None)
                        if loguru_tb:
                            loguru_tb.tb_next = new_tb
                        else:
                            root_tb = new_tb
                        loguru_tb = new_tb
                    tb = tb.tb_next

                caught_tb = root_tb

                while root_frame:
                    if root_frame.f_code.co_filename != __file__:
                        root_tb = loguru_traceback(root_frame, root_frame.f_lasti, root_frame.f_lineno, root_tb)
                    root_frame = root_frame.f_back

                if log_exception == 1:
                    caught_tb.__is_caught_point__ = True
                else:
                    tb_prev = tb_next = root_tb
                    while tb_next:
                        if tb_next == caught_tb:
                            break
                        tb_prev, tb_next = tb_next, tb_next.tb_next
                    tb_prev.__is_caught_point__ = True


                exception = (ex_type, ex, root_tb)

            record = {
                'name': name,
                'message': message,
                'time': now_,
                'elapsed': elapsed,
                'line': frame.f_lineno,
                'level': level_recattr,
                'file': file_recattr,
                'function': code.co_name,
                'module': splitext(file_name)[0],
                'thread': thread_recattr,
                'process': process_recattr,
                'exception': exception,
            }

            for _, handler in self.handlers.values():
                handler.emit(record)

        doc = "Log 'message.format(*args, **kwargs)' with severity '{}'.".format(level_name)
        if log_exception:
            doc += ' Log also current traceback.'
        log_function.__doc__ = doc

        return log_function

    trace = make_log_function.__func__(TRACE)
    debug = make_log_function.__func__(DEBUG)
    info = make_log_function.__func__(INFO)
    success = make_log_function.__func__(SUCCESS)
    warning = make_log_function.__func__(WARNING)
    error = make_log_function.__func__(ERROR)
    exception = make_log_function.__func__(ERROR, 1)
    _exception_catcher = make_log_function.__func__(ERROR, 2)
    critical = make_log_function.__func__(CRITICAL)

logger = Logger()
logger.log_to(STDERR)
