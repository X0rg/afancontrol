#!/usr/bin/env python3
"""
    afancontrol - Advanced fan speed control.

    This program lets you create complex configurations of fans speed control.
    Fans must support PWM control.
    Temperature sources might be files, HDDs, our own scripts.
    See config file for more info.

    Depends: hddtemp, lm_sensors

    Copyright 2013 Kostya Esmukov <kostya.shift@gmail.com>

    This file is part of afancontrol.

    afancontrol is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    afancontrol is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.

    You should have received a copy of the GNU General Public License
    along with afancontrol.  If not, see <http://www.gnu.org/licenses/>.

"""

import argparse
import configparser
import datetime
import os
import signal
import subprocess
import sys
from sys import exit
from time import sleep, time

DEFAULT_CONFIG = "/etc/afancontrol/afancontrol.conf"
DEFAULT_PIDFILE = "/var/run/afancontrol.pid"
DEFAULT_LOGFILE = None
DEFAULT_INTERVAL = 5
DEFAULT_FANS_SPEED_CHECK_INTERVAL = 3
DEFAULT_REPORT_CMD = 'printf "Subject: %s\nTo: %s\n\n%b" "afancontrol daemon report: %REASON%" root "%MESSAGE%" | sendmail -t'

DEFAULT_PANIC_ENTER_CMD = None
DEFAULT_PANIC_LEAVE_CMD = None
DEFAULT_THRESHOLD_ENTER_CMD = None
DEFAULT_THRESHOLD_LEAVE_CMD = None

DEFAULT_TEMP_PANIC_ENTER_CMD = None
DEFAULT_TEMP_PANIC_LEAVE_CMD = None
DEFAULT_TEMP_THRESHOLD_ENTER_CMD = None
DEFAULT_TEMP_THRESHOLD_LEAVE_CMD = None

DEFAULT_PWM_MIN = None
DEFAULT_PWM_MAX = None
DEFAULT_PWM_LINE_START = 100
DEFAULT_PWM_LINE_END = 240

DEFAULT_NEVER_STOP = True


class Log:
    """
    Simple Logger class
    """

    def __init__(self):
        self._background = False
        self._verbose = False
        self._logfile = None

    def initError(self, text):
        """Print error and terminate this program with error code 1"""
        not self._background and print(text, file=sys.stderr)
        # self._log("Init Error", text)
        exit(1)

    def error(self, text):
        not self._background and print(text, file=sys.stderr)
        self._log("Error", text)

    def warning(self, text):
        not self._background and print(text, file=sys.stderr)
        self._log("Warning", text)

    def info(self, text):
        not self._background and print(text, file=sys.stdout)
        self._log("Info", text)

    def debug(self, text):
        if self._verbose:
            not self._background and print(text, file=sys.stdout)
            self._log("Verbose", text)

    def background(self, background):
        """Set background mode (for forked process)"""
        self._background = background

    def verbose(self, verbose):
        """Set verbose mode"""
        self._verbose = verbose

    def logfile(self, logfile):
        """Set logfile path"""
        if self._logfile == logfile:
            return

        if self._logfile:
            self._log("Info", "Logfile closed")
        self._logfile = logfile
        if not self._log("Info", "Logfile started"):
            self._logfile = None
            self.error("Unable to write to logfile %s" % logfile)

    def _log(self, type, text):
        if not self._logfile:
            return None

        # We are not going to write log very often, so it will be better to open file on each write
        try:
            h = open(self._logfile, mode="at")
            contents = (
                "["
                + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                + "] (%s): %s\n" % (type, text)
            )
            h.write(contents)
            h.close()
        except:
            return False
        return True


class afancontrol_pwmfan:
    """
    PWM fan methods
    """

    MAX_PWM = 255
    MIN_PWM = 0
    STOP_PWM = 0

    def __init__(self, fan_data):
        self._pwm = fan_data["pwm"]
        self._fan_input = fan_data["fan_input"]

    def _write(self, filepath, contents):
        h = open(filepath, mode="wt")
        h.write(contents)
        h.close()

    def _read(self, filepath):
        h = open(filepath, mode="rt")
        t = h.read().strip()
        h.close()
        return t

    def get(self):
        """Get current PWM value"""
        return int(self._read(self._pwm))

    def set(self, pwm):
        """Set current PWM value (range 0~255)"""
        self._write(self._pwm, str(int(pwm)))

    def setFullSpeed(self):
        self.set(self.MAX_PWM)

    def enable(self):
        """Enable PWM control for this fan"""
        # fancontrol way of doing it
        if os.path.isfile(self._pwm + "_enable"):
            self._write(self._pwm + "_enable", "1")
        self._write(self._pwm, str(self.MAX_PWM))

    def disable(self):
        """Disable PWM control for this fan"""
        # fancontrol way of doing it
        pwm_enable = self._pwm + "_enable"
        if not os.path.isfile(pwm_enable):
            self._write(self._pwm, str(self.MAX_PWM))
            return

        self._write(pwm_enable, "0")
        if self._read(pwm_enable) == "0":
            return

        self._write(pwm_enable, "1")
        self._write(self._pwm, str(self.MAX_PWM))

        if (
            self._read(pwm_enable) == "1" and int(self._read(self._pwm)) == self.MAX_PWM
        ):  # >= 190
            return

        raise Exception("Out of luck disabling PWM on that fan.")

    def getSpeed(self):
        """Get current RPM for this fan"""
        return int(self._read(self._fan_input))


class afancontrol_temp:
    """
    Temperature sensors methods
    """

    def __init__(self, temp_data, env):
        self._d = temp_data
        self._env = env

        if self._d["type"] == "file":
            self._get = self._get_file
        elif self._d["type"] == "hdd":
            self._get = self._get_hdd
        elif self._d["type"] == "exec":
            self._get = self._get_exec
        else:
            raise Exception("Unknown type %s. Expected: file, hdd, exec." % d["type"])

    def _read(self, filepath):
        h = open(filepath, mode="rt")
        t = h.read().strip()
        h.close()
        return t

    def _get_file(self):
        temp = float(int(self._read(self._d["path"])) / 1000)

        try:
            min_t = float(self._d["min"])
            min_t += 0
        except:
            min_t = float(int(self._read(self._d["min"])) / 1000)

        try:
            max_t = float(self._d["max"])
            max_t += 0
        except:
            max_t = float(int(self._read(self._d["max"])) / 1000)

        return (temp, min_t, max_t)

    def _get_hdd(self):
        # Execute hddtemp, explode by newline, get maximum value, strip possible whitespaces, cast to float
        temp = float(
            max(
                afancontrol.execCommand(
                    self._env["hddtemp"] + " -n -u C %s" % self._d["path"]
                )
                .strip()
                .split("\n")
            ).strip()
        )

        min_t = float(self._d["min"])
        max_t = float(self._d["max"])

        return (temp, min_t, max_t)

    def _get_exec(self):
        t = self._exec(self._d["command"]).strip().split("\n")
        temp = float(t[0].strip())

        try:
            min_t = float(t[1].strip())
        except:
            min_t = float(self._d["min"])

        try:
            max_t = float(t[2].strip())
        except:
            max_t = float(self._d["max"])

        return (temp, min_t, max_t)

    def get(self):
        """Get current temperature"""
        ret = {}

        temp, min_t, max_t = self._get()

        ret["temp"] = temp
        ret["min"] = min_t
        ret["max"] = max_t

        if not (min_t < max_t):
            raise Exception(
                "Min temperature must be less than max. %s < %s" % (min_t, max_t)
            )

        ret["panic"] = self._d["panic"]
        ret["threshold"] = self._d["threshold"]

        ret["is_panic"] = self._d["panic"] != None and temp >= self._d["panic"]
        ret["is_threshold"] = (
            self._d["threshold"] != None and temp >= self._d["threshold"]
        )

        if temp < min_t:
            ret["speed"] = 0
        elif temp > max_t:
            ret["speed"] = 1
        else:
            ret["speed"] = (temp - min_t) / (max_t - min_t)

        return ret


class afancontrol_config:
    """
    Parse config file
    """

    def _getStr(self, s, d, key, default, mandatory=False):
        try:
            d[key] = s[key]
        except:
            if mandatory:
                raise Exception("%s is not set" % key)
            else:
                d[key] = default
        s.pop(key, None)

    def _getFloat(self, s, d, key, default, mandatory=False):
        try:
            d[key] = float(s[key])
        except:
            if mandatory:
                raise Exception("%s is not set" % key)
            else:
                d[key] = default
        s.pop(key, None)

    def _getBool(self, s, d, key, default, mandatory=False):
        try:
            d[key] = s.getboolean(key)
        except:
            if mandatory:
                raise Exception("%s is not set" % key)
            else:
                d[key] = default
        s.pop(key, None)

    def _getInt(self, s, d, key, default, mandatory=False):
        try:
            d[key] = int(s[key])
        except:
            if mandatory:
                raise Exception("%s is not set" % key)
            else:
                d[key] = default
        s.pop(key, None)

    def _parseTemp(self, d):
        info = {}

        self._getFloat(d, info, "panic", None)
        self._getFloat(d, info, "threshold", None)
        self._getStr(d, info, "panic_enter_cmd", DEFAULT_TEMP_PANIC_ENTER_CMD)
        self._getStr(d, info, "panic_leave_cmd", DEFAULT_TEMP_PANIC_LEAVE_CMD)
        self._getStr(d, info, "threshold_enter_cmd", DEFAULT_TEMP_THRESHOLD_ENTER_CMD)
        self._getStr(d, info, "threshold_leave_cmd", DEFAULT_TEMP_THRESHOLD_LEAVE_CMD)

        # min and max are not necessary
        self._getStr(d, info, "min", "", True)
        self._getStr(d, info, "max", "", True)

        self._getStr(d, info, "type", "", True)

        if info["type"] == "exec":
            self._getStr(d, info, "command", "", True)
        else:
            self._getStr(d, info, "path", "", True)

        if d:
            raise Exception("Unknown options: %s" % ", ".join(d.keys()))

        return info

    def _parseFan(self, d):
        info = {}

        self._getInt(d, info, "pwm_line_start", DEFAULT_PWM_LINE_START)
        self._getInt(d, info, "pwm_line_end", DEFAULT_PWM_LINE_END)
        self._getBool(d, info, "never_stop", DEFAULT_NEVER_STOP)

        for k in ["pwm_line_start", "pwm_line_end"]:
            if info[k] == None:
                continue
            if (info[k] < afancontrol_pwmfan.MIN_PWM) or (
                info[k] > afancontrol_pwmfan.MAX_PWM
            ):
                raise Exception(
                    "%s must be in range [%s;%s]. Got %s"
                    % (
                        k,
                        afancontrol_pwmfan.MIN_PWM,
                        afancontrol_pwmfan.MAX_PWM,
                        info[k],
                    )
                )

        for i in (("pwm_line_start", "pwm_line_end"),):
            if not (info[i[0]] < info[i[1]]):
                raise Exception(
                    "%s must be less than %s. Got %s < %s"
                    % (i[0], i[1], info[i[0]], info[i[1]])
                )

        self._getStr(d, info, "pwm", "", True)
        self._getStr(d, info, "fan_input", "", True)

        if d:
            raise Exception("Unknown options: %s" % ", ".join(d.keys()))

        return info

    def _parseMapping(self, d):
        s = {}
        info = {"fans": {}, "temps": {}}
        self._getStr(d, s, "fans", "", True)
        self._getStr(d, s, "temps", "", True)

        for n in s["temps"].split(","):
            n = n.strip()
            if n == "":
                continue
            if n in info["temps"]:
                raise Exception("Duplicate temp %s" % n)
            info["temps"][n] = True

        for nm in s["fans"].split(","):
            nm = nm.split("*")

            n = nm[0].strip()
            if n == "":
                continue

            try:
                m = nm[1]
            except:
                m = 1

            try:
                m = float(m)
            except Exception as ex:
                raise Exception("bad multiplier for fan %s:\n%s" % (n, ex))

            if n in info["fans"]:
                raise Exception("Duplicate fan %s" % n)
            info["fans"][n] = m
        return info

    def _parseDaemon(self, d, args):
        info = {}
        info["pidfile"] = args.pidfile
        if not info["pidfile"]:
            self._getStr(d, info, "pidfile", DEFAULT_PIDFILE)
        d.pop("pidfile", None)

        info["logfile"] = args.logfile
        if not info["logfile"]:
            self._getStr(d, info, "logfile", DEFAULT_LOGFILE)
        d.pop("logfile", None)

        self._getInt(d, info, "interval", DEFAULT_INTERVAL)
        self._getInt(
            d, info, "fans_speed_check_interval", DEFAULT_FANS_SPEED_CHECK_INTERVAL
        )

        try:
            info["hddtemp"] = d["hddtemp"]
        except:
            info["hddtemp"] = afancontrol.execCommand("which hddtemp").strip()
        d.pop("hddtemp", None)

        if not info["hddtemp"]:
            raise Exception("Unable to find hddtemp")

        if d:
            raise Exception("Unknown options: %s" % ", ".join(d.keys()))

        return info

    def _parseActions(self, d):
        info = {}

        self._getStr(d, info, "report_cmd", DEFAULT_REPORT_CMD)
        self._getStr(d, info, "panic_enter_cmd", DEFAULT_PANIC_ENTER_CMD)
        self._getStr(d, info, "panic_leave_cmd", DEFAULT_PANIC_LEAVE_CMD)
        self._getStr(d, info, "threshold_enter_cmd", DEFAULT_THRESHOLD_ENTER_CMD)
        self._getStr(d, info, "threshold_leave_cmd", DEFAULT_THRESHOLD_LEAVE_CMD)

        if d:
            raise Exception("Unknown options: %s" % ", ".join(d.keys()))

        return info

    def parse(self, args):
        """
        Parse config to dict.

        Keep in mind that configparser returns dict-like object, but it is not a dict.
        So we should create new usual dict and copy values from configparser to it
        """
        config = configparser.ConfigParser(interpolation=None)
        try:
            config.read(args.config)
        except Exception as ex:
            raise Exception("Unable to parse %s:\n%s" % (args.config, ex))

        if not config.sections():
            raise Exception(
                "File is empty, not readable or doesn't exists: %s" % args.config
            )

        sections = config.sections()
        res = {"daemon": {}, "actions": {}, "temps": {}, "fans": {}, "mappings": {}}

        for sect in sections:
            vals = config[sect]
            try:
                if sect.lower().strip() == "daemon":
                    res["daemon"] = self._parseDaemon(vals, args)
                elif sect.lower().strip() == "actions":
                    res["actions"] = self._parseActions(vals)
                else:
                    m = sect.split(":", 1)
                    ms_sect = m[0].strip().lower()

                    if ms_sect == "temp":
                        ms_f = self._parseTemp
                    elif ms_sect == "fan":
                        ms_f = self._parseFan
                    elif ms_sect == "mapping":
                        ms_f = self._parseMapping
                    else:
                        raise Exception("Unknown section %s" % sect)

                    try:
                        ms_name = m[1].strip()
                    except:
                        raise Exception(
                            "Section %s must have colon folowed by name" % ms_sect
                        )

                    if ms_name in res["%ss" % ms_sect]:
                        raise Exception(
                            "Duplicate name %s for section %s" % (ms_name, ms_sect)
                        )

                    res["%ss" % ms_sect][ms_name] = ms_f(vals)
            except Exception as ex:
                raise Exception("Failed to process %s section:\n%s" % (sect, ex))

        unused = {"fans": res["fans"].copy(), "temps": res["temps"].copy()}

        for map_name in res["mappings"]:
            for s in ("temp", "fan"):
                for s_name in res["mappings"][map_name]["%ss" % s]:
                    unused["%ss" % s].pop(s_name, None)
                    if not (s_name in res["%ss" % s]):
                        raise Exception(
                            "Unknown %s %s in mapping %s" % (s, s_name, map_name)
                        )

        for s in ("temps", "fans"):
            if unused[s]:
                raise Exception(
                    "Some %s are set but not used in mappings:\n%s"
                    % (s, ", ".join(unused[s].keys()))
                )

        return res


class afancontrol:
    """
    Main worker class
    """

    def __init__(self, args):
        """Init instance"""
        self._fans_pwmed = False  # are fans prepared or not
        self._background = False  # are we running background
        self._hup_queued = False  # is hup queued

        # Tick vars:
        self._events = {
            "panic": {
                "state": False,  # are we working in panic mode
                "temps": {},  # Dict of sensors that reached panic temp
            },
            "threshold": {
                "state": False,  # are we working in threshold mode
                "temps": {},  # Dict of sensors that reached threshold temp
            },
        }

        self._failed_fans = {}  # Dict of fans marked as failing (which speed is 0)
        self._stopped_fans = {}  # Dict of fans that will be skipped on speed check
        self._fans_check_ts = 0  # Timestamp of last fans check

        self._args = args

        self._l = Log()

        if args.verbose:
            self._l.verbose(True)

        try:
            self._c = afancontrol_config().parse(self._args)
        except Exception as ex:
            self._l.initError("Config parsing failed:\n%s" % ex)

        self._fans = {}
        self._temps = {}

        try:
            self._init_fanstemps()
        except Exception as ex:
            self._l.initError(ex)

        if args.test:
            print("Config file is good")
            exit(0)

        try:
            self._checkPid()

            # Test pidfile. We shouldn't fork in case it is not writable.
            self._savePid(os.getpid())
        except Exception as ex:
            self._l.initError(ex)

        if self._c["daemon"]["logfile"]:
            self._l.logfile(self._c["daemon"]["logfile"])

    def prepare(self):
        """Prepare program to run"""

        # We are properly initialized here.
        # Starting our work.

        self._prepareFans()

        # Now we have to be careful and make sure to call self._restoreFans() before exit

        # Validate config by running first tick
        try:
            self._tick()
        except Exception as ex:
            self._l.error("Fancontrol tick failed: %s" % ex)
            self.exit(1)

        # Fork
        if self._args.daemon:
            child_pid = os.fork()
            if child_pid != 0:
                try:
                    self._savePid(child_pid)
                except Exception as ex:
                    self._l.initError(ex)
                exit(0)
            self._l.background(True)
            self._background = True

    def _init_fanstemps(self):
        """Create instances of afancontrol_pwmfan and afancontrol_temp classes"""

        for (n, d) in self._c["fans"].items():
            try:
                self._fans[n] = afancontrol_pwmfan(d)
            except Exception as ex:
                raise Exception("Fan %s initialization failed:\n%s" % (n, ex))

        temp_env = {"hddtemp": self._c["daemon"]["hddtemp"]}

        for (n, d) in self._c["temps"].items():
            try:
                self._temps[n] = afancontrol_temp(d, temp_env)
            except Exception as ex:
                raise Exception("Temp %s initialization failed:\n%s" % (n, ex))

    @staticmethod
    def execCommand(cmd, stderr=False):
        """Static method for all classes to execute commands in system shell"""

        devnull = open(os.devnull, "wb") if not stderr else subprocess.PIPE
        i = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=devnull, close_fds=True
        )
        r = i.stdout.read().decode("utf-8")
        i.stdout.close()
        ec = i.wait()
        if not stderr:
            devnull.close()
        if ec != 0:
            raise Exception(
                "Got non-zero exitcode (%s) while was trying to execute %s. Output:\n%s"
                % (ec, cmd, r)
            )
        return r

    def process(self):
        """Main worker function. Runs in infinite loop"""
        while True:
            # Docs: any caught signal will terminate the sleep()
            if not self._hup_queued:
                sleep(self._c["daemon"]["interval"])

            if self._hup_queued:
                self._hup_queued = False
                self._hup()
            try:
                self._tick()
            except Exception as ex:
                self._report("Tick failed", "Fancontrol tick failed: %s" % ex)

    def _tick_temps(self):
        """Tick helper. Reads temp info from all sensors"""
        temps = {}
        p_temps = {}
        t_temps = {}

        for (n, i) in self._temps.items():
            try:
                info = i.get()
                temps[n] = info
                self._l.debug(
                    "id: %s, temp: %s, speed: %s, panic: %s, threshold: %s"
                    % (n, info["temp"], info["speed"], info["panic"], info["threshold"])
                )
            except Exception as ex:
                # For failed sensors we assume info equal to False and set sensor as panic
                temps[n] = False
                p_temps[n] = "Sensor failed: %s" % ex
                self._l.warning("Temp sensor failed: %s" % ex)
                continue

            if info["is_panic"]:
                p_temps[n] = "Panic temp reached"
            if info["is_threshold"]:
                t_temps[n] = "Threshold temp reached"

        return (temps, p_temps, t_temps)

    def _tick_checkSpeeds(self):
        """Tick helper. Checks speeds for every fan"""

        # We set fans list to check in current tick, check them on the next
        # Fans need some time (~2 seconds) to stop or start.

        cts = time()
        if cts > self._fans_check_ts + self._c["daemon"]["fans_speed_check_interval"]:
            for (n, i) in self._fans.items():
                if n in self._stopped_fans:
                    continue
                try:
                    if i.getSpeed() == 0:
                        raise Exception("Fan speed is 0")

                    if n in self._failed_fans:
                        self._report(
                            "fan started: %s" % n,
                            "Fan %s which had been reported as failing have just started."
                            % n,
                        )
                        del self._failed_fans[n]

                except Exception as ex:
                    if n in self._failed_fans:
                        continue
                    self._failed_fans[n] = True

                    try:
                        i.setFullSpeed()
                        s = "Fan set to full speed"
                    except Exception as ex:
                        s = "Setting fan to full speed failed:\n%s" % ex

                    self._report(
                        "fan stopped: %s" % n,
                        "Seems to me that fan %s is failing:\n%s\n\n%s" % (n, ex, s),
                    )

            self._fans_check_ts = cts

        self._stopped_fans = {}

    def _tick_evCmd(self, cmd, name):
        try:
            if cmd == None:
                return
            r = afancontrol.execCommand(cmd, True)
            if r:
                self._l.info("%s returned:\n%s" % (name, r))
        except Exception as ex:
            self._l.warning("Unable to execute %s:\n%s" % (name, ex))

    def _tick_ev(self, temps, event_temps, event, global_events):
        """
        Tick helper. It does all that mess around panic and threshold events.
        Returns boolean indicating should we set fans to full speed or not

        Should be run for every event:
        self._tick_ev(temps, p_temps, 'panic', self._events['panic'])
        self._tick_ev(temps, t_temps, 'threshold', self._events['threshold'])
        """
        out_of = global_events["temps"].copy()

        ret = False  # should fans be set to full speed

        if event_temps:
            for (n, reason) in event_temps.items():
                # Unset temps that are still present
                if n in global_events["temps"]:
                    out_of.pop(n, None)
                    continue

                global_events["temps"][n] = True

                # log
                info = temps[n]
                try:
                    s = (
                        "%s started on temp: id: %s, current_temp: %s, %s_temp: %s, reason: %s"
                        % (event.upper(), n, info["temp"], event, info[event], reason)
                    )
                except:
                    s = "%s started on temp: id: %s, reason: %s" % (
                        event.upper(),
                        n,
                        reason,
                    )
                self._l.warning(s)

                self._tick_evCmd(
                    self._c["temps"][n]["%s_enter_cmd" % event],
                    "%s_enter_cmd for %s" % (event, n),
                )

            if not global_events["state"] and event_temps:
                global_events["state"] = True

                # make list of sensors
                l = []
                for (n, info) in temps.items():
                    try:
                        s = (
                            "[%s] current_temp: %s, panic_temp: %s, threshold_temp: %s"
                            % (n, info["temp"], info["panic"], info["threshold"])
                        )
                    except:
                        s = "[%s] n/a" % (n)
                    l.append(s)

                # call report
                self._report(
                    "Entered %s MODE" % event.upper(),
                    "Entered %s MODE. Take a look as soon as possible!!!\nSensors:\n%s"
                    % (event.upper(), "\n".join(l)),
                )

                self._tick_evCmd(
                    self._c["actions"]["%s_enter_cmd" % event], "%s_enter_cmd" % event
                )

            ret = True

        for n in out_of:
            global_events["temps"].pop(n, None)

            info = temps[n]
            self._l.info(
                "%s ended on temp: id: %s, current_temp: %s, %s_temp: %s"
                % (event.upper(), n, info["temp"], event, info[event])
            )

            self._tick_evCmd(
                self._c["temps"][n]["%s_leave_cmd" % event],
                "%s_leave_cmd for %s" % (event, n),
            )

        if global_events["state"] and not event_temps:
            global_events["state"] = False

            # make list of sensors
            l = []
            for (n, info) in temps.items():
                try:
                    s = "[%s] current_temp: %s, panic_temp: %s, threshold_temp: %s" % (
                        n,
                        info["temp"],
                        info["panic"],
                        info["threshold"],
                    )
                except:
                    s = "[%s] n/a" % (n)
                l.append(s)

            self._report(
                "Leaving %s MODE" % event.upper(),
                "Leaving %s MODE. Sensors:\n%s" % (event.upper(), "\n".join(l)),
            )

            self._tick_evCmd(
                self._c["actions"]["%s_leave_cmd" % event], "%s_leave_cmd" % event
            )

        return ret

    def _tick_fullFanSpeed(self):
        """Tick helper. Set all fans to full speed"""
        for (n, i) in self._fans.items():
            if n in self._failed_fans:
                continue
            try:
                i.setFullSpeed()
            except Exception as ex:
                self._l.warning("Unable to set fan %s to fullspeed:\n%s" % (n, ex))

    def _tick_setFanSpeeds(self, temps):
        """Tick helper. Count fan speeds using temps and mappings and set them"""
        fans = {}

        for (n, mapping) in self._c["mappings"].items():
            # Find highest speed from all temps of current mapping
            mapspeed = None
            for tn in mapping["temps"]:
                s = temps[tn]["speed"]
                if (mapspeed == None) or (mapspeed < s):
                    mapspeed = s

            for (fn, m) in mapping["fans"].items():
                if fn in fans:
                    fans[fn] += mapspeed * m
                else:
                    fans[fn] = mapspeed * m

        for (n, i) in self._fans.items():
            if n in self._failed_fans:
                continue

            if n in fans:
                m = fans[n]
            else:
                m = 0

            if m > 1:
                self._l.debug(
                    "Speed for fan %s is too high. %s truncated to 1." % (n, m)
                )
                m = 1

            pwm = m * self._c["fans"][n]["pwm_line_end"]

            if ((pwm != 0) and (pwm < self._c["fans"][n]["pwm_line_start"])) or (
                self._c["fans"][n]["never_stop"] and (pwm == 0)
            ):
                pwm = self._c["fans"][n]["pwm_line_start"]

            if pwm == 0:
                self._stopped_fans[n] = True

            try:
                self._l.debug("Fan: %s, speed: %s, pwm: %s" % (n, m, pwm))
                i.set(pwm)
            except Exception as ex:
                self._l.warning("Unable to set fan %s to speed %s:\n%s" % (n, pwm, ex))

    def _tick(self):
        """Tick. This function does all this work about fans regulation."""

        # Get temps from all sensors
        temps, p_temps, t_temps = self._tick_temps()

        # Check speeds of every fan
        self._tick_checkSpeeds()

        # Process panic and threshold events
        if self._tick_ev(
            temps, p_temps, "panic", self._events["panic"]
        ) or self._tick_ev(temps, t_temps, "threshold", self._events["threshold"]):
            # If we got one - set fans to full speed
            self._tick_fullFanSpeed()
        else:
            self._tick_setFanSpeeds(temps)

    def _prepareFans(self):
        """Prepare fans to work with PWM"""
        if self._fans_pwmed:
            return False

        self._fans_pwmed = True

        self._l.info("Enabling PWM on fans...")
        for (n, i) in self._fans.items():
            try:
                i.enable()
            except Exception as ex:
                self._l.error(
                    "Exception occured while was trying to enable %s:\n%s" % (n, ex)
                )
                self.exit(1)
        return True

    def _restoreFans(self):
        """Disable PWM for fans"""
        if not self._fans_pwmed:
            return False

        self._fans_pwmed = False

        self._l.info("Disabling PWM on fans...")
        for (n, i) in self._fans.items():
            try:
                i.disable()
            except Exception as ex:
                self._l.warning(
                    "Exception occured while was trying to disable %s. Please make sure that this fan is running full speed. Exception:\n%s"
                    % (pwm, ex)
                )
        self._l.info("Done. Verify fans have returned to full speed")

        return True

    def _report(self, reason, message):
        """Execute report command"""
        self._l.info("[REPORT] Reason: %s. Message: %s" % (reason, message))
        try:
            rc = self._c["actions"]["report_cmd"]
            rc = rc.replace("%REASON%", reason)
            rc = rc.replace("%MESSAGE%", message)
            self.execCommand(rc).strip()
        except Exception as ex:
            self._l.warning("Report failed: %s" % ex)

    def _checkPid(self):
        """Check whenever pidfile is present"""
        if os.path.isfile(self._c["daemon"]["pidfile"]):
            raise Exception(
                "Pidfile [%s] already exists. Is daemon running? Remove this file overwise."
                % self._c["daemon"]["pidfile"]
            )

    def _savePid(self, pid):
        """Save pid to pidfile"""
        try:
            h = open(self._c["daemon"]["pidfile"], mode="wt")
            h.write(str(pid))
            h.close()
        except Exception as ex:
            raise Exception(
                "Unable to create pid file %s:\n%s" % (self._c["daemon"]["pidfile"], ex)
            )

    def _deletePid(self, pidfile=None):
        """Deletes pidfile"""
        if pidfile == None:
            pidfile = self._c["daemon"]["pidfile"]
        try:
            os.remove(pidfile)
        except:
            raise Exception("Unable to delete pidfile %s" % pidfile)

    def queueHup(self):
        """Queues HUP signal"""
        # Signals are called asynchroniously, so we queue hup signal to avoid tick corruption
        self._hup_queued = True
        self._l.info("SIGHUP have been queued")

    def _hup(self):
        """HUP signal processor"""
        if not self._background:
            self.exit(0)
        else:
            # This is not realtime service like a web server,
            # so instead of reloading (SIGHUP) you should just restart the program.
            # Of course it's possible to handle SIGHUP and properly reload config,
            # but i'm so lazy to implement this. I'm sorry.

            self._l.info("SIGHUP ignored due to running in daemon mode")

    def exit(self, code):
        """Corectly terminates program"""

        # No code will be executed after this method.
        # It's OK to call this function during tick, because fans will be reset and program will terminate.

        self._restoreFans()
        try:
            self._deletePid()
        except Exception as ex:
            self._l.warning(ex)
            exit(1)

        self._l.info("Process gracefully stopped")
        exit(code)


def cleanup(signum, stackframe):
    global afc
    afc.exit(0)


def sighup(signum, stackframe):
    global afc
    afc.queueHup()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-t", "--test", help="test config", action="store_true")
    parser.add_argument(
        "-d", "--daemon", help="execute in daemon mode", action="store_true"
    )
    parser.add_argument(
        "-v", "--verbose", help="increase output verbosity", action="store_true"
    )
    parser.add_argument(
        "-c",
        "--config",
        help="config path [%s]" % DEFAULT_CONFIG,
        default=DEFAULT_CONFIG,
    )
    parser.add_argument("--pidfile", help="pidfile path [%s]" % DEFAULT_PIDFILE)
    parser.add_argument("--logfile", help="logfile path [%s]" % DEFAULT_LOGFILE)
    args = parser.parse_args()

    afc = afancontrol(args)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGQUIT, cleanup)
    signal.signal(signal.SIGINT, cleanup)
    signal.signal(signal.SIGHUP, sighup)

    afc.prepare()
    afc.process()


if __name__ == "__main__":
    main()