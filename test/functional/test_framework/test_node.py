#!/usr/bin/env python3
# Copyright (c) 2017-2018 The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Class for bitcoind node under test"""

import contextlib
import decimal
import errno
from enum import Enum
import http.client
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
import collections

from .authproxy import JSONRPCException
from .util import (
    append_config,
    delete_cookie_file,
    get_rpc_proxy,
    rpc_url,
    wait_until,
    p2p_port,
)

# For Python 3.4 compatibility
JSONDecodeError = getattr(json, "JSONDecodeError", ValueError)

BITCOIND_PROC_WAIT_TIMEOUT = 60


class FailedToStartError(Exception):
    """Raised when a node fails to start correctly."""


class ErrorMatch(Enum):
    FULL_TEXT = 1
    FULL_REGEX = 2
    PARTIAL_REGEX = 3


class TestNode():
    """A class for representing a bitcoind node under test.

    This class contains:

    - state about the node (whether it's running, etc)
    - a Python subprocess.Popen object representing the running process
    - an RPC connection to the node
    - one or more P2P connections to the node


    To make things easier for the test writer, any unrecognised messages will
    be dispatched to the RPC connection."""

    def __init__(self, i, datadir, chain, *, rpchost, timewait, bitcoind, bitcoin_cli, mocktime, coverage_dir, extra_conf=None, extra_args=None, use_cli=False, chain_in_args=True):
        self.index = i
        self.datadir = datadir
        self.stdout_dir = os.path.join(self.datadir, "stdout")
        self.stderr_dir = os.path.join(self.datadir, "stderr")
        self.chain = chain
        self.rpchost = rpchost
        self.rpc_timeout = timewait
        self.binary = bitcoind
        self.coverage_dir = coverage_dir
        if extra_conf != None:
            append_config(datadir, extra_conf)
        # Most callers will just need to add extra args to the standard list below.
        # For those callers that need more flexibility, they can just set the args property directly.
        # Note that common args are set in the config file (see initialize_datadir)
        self.extra_args = extra_args
        self.args = [
            self.binary,
            "-datadir=" + self.datadir,
            "-logtimemicros",
            "-debug",
            "-debugexclude=libevent",
            "-debugexclude=leveldb",
            "-mocktime=" + str(mocktime),
            "-uacomment=testnode%d" % i
        ]
        if chain_in_args:
            self.args += ["-chain=" + self.chain]

        self.cli = TestNodeCLI(bitcoin_cli, self.datadir, self.chain)
        self.use_cli = use_cli

        self.running = False
        self.process = None
        self.rpc_connected = False
        self.rpc = None
        self.url = None
        self.log = logging.getLogger('TestFramework.node%d' % i)
        self.cleanup_on_exit = True # Whether to kill the node when this object goes away

        self.p2ps = []

        # ELEMENTS:
        self.deterministic_priv_key = None

    def set_deterministic_priv_key(self, address, privkey):
        AddressKeyPair = collections.namedtuple('AddressKeyPair', ['address', 'key'])
        self.deterministic_priv_key = AddressKeyPair(address, privkey)

    def get_deterministic_priv_key(self):
        """Return a deterministic priv key in base58, that only depends on the node's index"""

        AddressKeyPair = collections.namedtuple('AddressKeyPair', ['address', 'key'])
        PRIV_KEYS = [
            # address , privkey
            AddressKeyPair('2doncj41FX6LahE2aspuLuQcnmgrLtfvvma', 'cQ1PxVTn5J3qCzUqXzpNeHiPVxZcwwaJkFmZcokescXwmHWAdBoe'),
            AddressKeyPair('2dv4oTKjmi3TF6dRq2TmasMdNtTeuqHNcPb', 'cRuGzyZjb5zQQg1TbAiGK1UJBuK1UQHaFf4DXBUcPZNZ3WomNoxW'),
            AddressKeyPair('2drNifUyWj5D8UPrAJtuQjUyPSUYpE4gb7E', 'cSYxv1JKDNrSKnm2fzNj1uFUkr7HqTQHvU8PCAZ1mWNwYB5LwZVu'),
            AddressKeyPair('2dwTDXu4QLFG61upW7wkRLbkUxDRYuRmnAd', 'cQLvSojQFLkikwuzhSkKPv9REWpCDNhvGiG5hjutYPQj4HT8GnJy'),
            AddressKeyPair('2doqDLuXpHx3x6Bd9bBAWxRzzKZCXTbSCWE', 'cNkbdkyQ9RX3Yrp4oFACs4p4iiyBxoC3pL7zioGKSzmvyLbiR4Rm'),
            AddressKeyPair('2dktmJxjtpKEftBKFaGBUCW7wsUEBGeuoSi', 'cSCCVU7iUXMNnqrHPeVxHEgG48TsyNwd3FAsS2hjTYKdTSNDXJXV'),
            AddressKeyPair('2dxjmBn21SQjhffXzzi1hz5nKA4quM7jtsT', 'cVpzWr59KE7DsWaSJkySSPSvkrr6huUsjYBF3wcwCMhW4cEPNmU1'),
            AddressKeyPair('2dpFhgNeWqm7LVbvZo29bvXPEzwWTVfzVSY', 'cRPF2Kfm21BWf3GPHMKGGMQN1a6sNJPGsyYSz8VWQhsoVUr42q4r'),
            AddressKeyPair('2df8FzAJJtPcHsvXYRh4BTmnhSUUVE5zNhE', 'cU9yBKyGyRNBpzSmVavoh9szgaFUjKbPG9P3CPtycXAxmdwnKxiL'),
        ]

        # ELEMENTS: this allows overriding the default for parent nodes in fedpeg test
        if self.deterministic_priv_key is not None:
            self.log.debug("Custom deterministic_priv_key: {}".format(self.deterministic_priv_key))
            return self.deterministic_priv_key

        return PRIV_KEYS[self.index]

    def get_mem_rss(self):
        """Get the memory usage (RSS) per `ps`.

        Returns None if `ps` is unavailable.
        """
        assert self.running

        try:
            return int(subprocess.check_output(
                ["ps", "h", "-o", "rss", "{}".format(self.process.pid)],
                stderr=subprocess.DEVNULL).split()[-1])

        # Avoid failing on platforms where ps isn't installed.
        #
        # We could later use something like `psutils` to work across platforms.
        except (FileNotFoundError, subprocess.SubprocessError):
            self.log.exception("Unable to get memory usage")
            return None

    def _node_msg(self, msg: str) -> str:
        """Return a modified msg that identifies this node by its index as a debugging aid."""
        return "[node %d] %s" % (self.index, msg)

    def _raise_assertion_error(self, msg: str):
        """Raise an AssertionError with msg modified to identify this node."""
        raise AssertionError(self._node_msg(msg))

    def __del__(self):
        # Ensure that we don't leave any bitcoind processes lying around after
        # the test ends
        if self.process and self.cleanup_on_exit:
            # Should only happen on test failure
            # Avoid using logger, as that may have already been shutdown when
            # this destructor is called.
            print(self._node_msg("Cleaning up leftover process"))
            self.process.kill()

    def __getattr__(self, name):
        """Dispatches any unrecognised messages to the RPC connection or a CLI instance."""
        if self.use_cli:
            return getattr(self.cli, name)
        else:
            assert self.rpc_connected and self.rpc is not None, self._node_msg("Error: no RPC connection")
            return getattr(self.rpc, name)

    def start(self, extra_args=None, *, stdout=None, stderr=None, **kwargs):
        """Start the node."""
        if extra_args is None:
            extra_args = self.extra_args

        # Add a new stdout and stderr file each time bitcoind is started
        if stderr is None:
            stderr = tempfile.NamedTemporaryFile(dir=self.stderr_dir, delete=False)
        if stdout is None:
            stdout = tempfile.NamedTemporaryFile(dir=self.stdout_dir, delete=False)
        self.stderr = stderr
        self.stdout = stdout

        # Delete any existing cookie file -- if such a file exists (eg due to
        # unclean shutdown), it will get overwritten anyway by bitcoind, and
        # potentially interfere with our attempt to authenticate
        delete_cookie_file(self.datadir, self.chain)

        # add environment variable LIBC_FATAL_STDERR_=1 so that libc errors are written to stderr and not the terminal
        subp_env = dict(os.environ, LIBC_FATAL_STDERR_="1")

        self.process = subprocess.Popen(self.args + extra_args, env=subp_env, stdout=stdout, stderr=stderr, **kwargs)

        self.running = True
        self.log.debug("bitcoind started, waiting for RPC to come up")

    def wait_for_rpc_connection(self):
        """Sets up an RPC connection to the bitcoind process. Returns False if unable to connect."""
        # Poll at a rate of four times per second
        poll_per_s = 4
        for _ in range(poll_per_s * self.rpc_timeout):
            if self.process.poll() is not None:
                raise FailedToStartError(self._node_msg(
                    'bitcoind exited with status {} during initialization'.format(self.process.returncode)))
            try:
                self.rpc = get_rpc_proxy(rpc_url(self.datadir, self.index, self.chain, self.rpchost), self.index, timeout=self.rpc_timeout, coveragedir=self.coverage_dir)
                self.rpc.getblockcount()
                # If the call to getblockcount() succeeds then the RPC connection is up
                self.rpc_connected = True
                self.url = self.rpc.url
                self.log.debug("RPC successfully started")
                return
            except IOError as e:
                if e.errno != errno.ECONNREFUSED:  # Port not yet open?
                    raise  # unknown IO error
            except JSONRPCException as e:  # Initialization phase
                # -28 RPC in warmup
                # -342 Service unavailable, RPC server started but is shutting down due to error
                if e.error['code'] != -28 and e.error['code'] != -342:
                    raise  # unknown JSON RPC exception
            except ValueError as e:  # cookie file not found and no rpcuser or rpcassword. bitcoind still starting
                if "No RPC credentials" not in str(e):
                    raise
            time.sleep(1.0 / poll_per_s)
        self._raise_assertion_error("Unable to connect to bitcoind")

    def generate(self, nblocks, maxtries=1000000):
        self.log.debug("TestNode.generate() dispatches `generate` call to `generatetoaddress`")
        return self.generatetoaddress(nblocks=nblocks, address=self.get_deterministic_priv_key().address, maxtries=maxtries)

    def get_wallet_rpc(self, wallet_name):
        if self.use_cli:
            return self.cli("-rpcwallet={}".format(wallet_name))
        else:
            assert self.rpc_connected and self.rpc, self._node_msg("RPC not connected")
            wallet_path = "wallet/{}".format(urllib.parse.quote(wallet_name))
            return self.rpc / wallet_path

    def stop_node(self, expected_stderr=''):
        """Stop the node."""
        if not self.running:
            return
        self.log.debug("Stopping node")
        try:
            self.stop()
        except http.client.CannotSendRequest:
            self.log.exception("Unable to stop node.")

        # Check that stderr is as expected
        self.stderr.seek(0)
        stderr = self.stderr.read().decode('utf-8').strip()
        if stderr != expected_stderr:
            raise AssertionError("Unexpected stderr {} != {}".format(stderr, expected_stderr))

        self.stdout.close()
        self.stderr.close()

        del self.p2ps[:]

    def is_node_stopped(self):
        """Checks whether the node has stopped.

        Returns True if the node has stopped. False otherwise.
        This method is responsible for freeing resources (self.process)."""
        if not self.running:
            return True
        return_code = self.process.poll()
        if return_code is None:
            return False

        # process has stopped. Assert that it didn't return an error code.
        assert return_code == 0, self._node_msg(
            "Node returned non-zero exit code (%d) when stopping" % return_code)
        self.running = False
        self.process = None
        self.rpc_connected = False
        self.rpc = None
        self.log.debug("Node stopped")
        return True

    def wait_until_stopped(self, timeout=BITCOIND_PROC_WAIT_TIMEOUT):
        wait_until(self.is_node_stopped, timeout=timeout)

    @contextlib.contextmanager
    def assert_debug_log(self, expected_msgs):
        debug_log = os.path.join(self.datadir, self.chain, 'debug.log')
        with open(debug_log, encoding='utf-8') as dl:
            dl.seek(0, 2)
            prev_size = dl.tell()
        try:
            yield
        finally:
            with open(debug_log, encoding='utf-8') as dl:
                dl.seek(prev_size)
                log = dl.read()
            print_log = " - " + "\n - ".join(log.splitlines())
            for expected_msg in expected_msgs:
                if re.search(re.escape(expected_msg), log, flags=re.MULTILINE) is None:
                    self._raise_assertion_error('Expected message "{}" does not partially match log:\n\n{}\n\n'.format(expected_msg, print_log))

    @contextlib.contextmanager
    def assert_memory_usage_stable(self, perc_increase_allowed=0.03):
        """Context manager that allows the user to assert that a node's memory usage (RSS)
        hasn't increased beyond some threshold percentage.
        """
        before_memory_usage = self.get_mem_rss()

        yield

        after_memory_usage = self.get_mem_rss()

        if not (before_memory_usage and after_memory_usage):
            self.log.warning("Unable to detect memory usage (RSS) - skipping memory check.")
            return

        perc_increase_memory_usage = (after_memory_usage / before_memory_usage) - 1

        if perc_increase_memory_usage > perc_increase_allowed:
            self._raise_assertion_error(
                "Memory usage increased over threshold of {:.3f}% from {} to {} ({:.3f}%)".format(
                    perc_increase_allowed * 100, before_memory_usage, after_memory_usage,
                    perc_increase_memory_usage * 100))

    def assert_start_raises_init_error(self, extra_args=None, expected_msg=None, match=ErrorMatch.FULL_TEXT, *args, **kwargs):
        """Attempt to start the node and expect it to raise an error.

        extra_args: extra arguments to pass through to bitcoind
        expected_msg: regex that stderr should match when bitcoind fails

        Will throw if bitcoind starts without an error.
        Will throw if an expected_msg is provided and it does not match bitcoind's stdout."""
        with tempfile.NamedTemporaryFile(dir=self.stderr_dir, delete=False) as log_stderr, \
             tempfile.NamedTemporaryFile(dir=self.stdout_dir, delete=False) as log_stdout:
            try:
                self.start(extra_args, stdout=log_stdout, stderr=log_stderr, *args, **kwargs)
                self.wait_for_rpc_connection()
                self.stop_node()
                self.wait_until_stopped()
            except FailedToStartError as e:
                self.log.debug('bitcoind failed to start: %s', e)
                self.running = False
                self.process = None
                # Check stderr for expected message
                if expected_msg is not None:
                    log_stderr.seek(0)
                    stderr = log_stderr.read().decode('utf-8').strip()
                    if match == ErrorMatch.PARTIAL_REGEX:
                        if re.search(expected_msg, stderr, flags=re.MULTILINE) is None:
                            self._raise_assertion_error(
                                'Expected message "{}" does not partially match stderr:\n"{}"'.format(expected_msg, stderr))
                    elif match == ErrorMatch.FULL_REGEX:
                        if re.fullmatch(expected_msg, stderr) is None:
                            self._raise_assertion_error(
                                'Expected message "{}" does not fully match stderr:\n"{}"'.format(expected_msg, stderr))
                    elif match == ErrorMatch.FULL_TEXT:
                        if expected_msg != stderr:
                            self._raise_assertion_error(
                                'Expected message "{}" does not fully match stderr:\n"{}"'.format(expected_msg, stderr))
            else:
                if expected_msg is None:
                    assert_msg = "bitcoind should have exited with an error"
                else:
                    assert_msg = "bitcoind should have exited with expected error " + expected_msg
                self._raise_assertion_error(assert_msg)

    def add_p2p_connection(self, p2p_conn, *, wait_for_verack=True, **kwargs):
        """Add a p2p connection to the node.

        This method adds the p2p connection to the self.p2ps list and also
        returns the connection to the caller."""
        if 'dstport' not in kwargs:
            kwargs['dstport'] = p2p_port(self.index)
        if 'dstaddr' not in kwargs:
            kwargs['dstaddr'] = '127.0.0.1'

        p2p_conn.peer_connect(**kwargs)()
        self.p2ps.append(p2p_conn)
        if wait_for_verack:
            p2p_conn.wait_for_verack()

        return p2p_conn

    @property
    def p2p(self):
        """Return the first p2p connection

        Convenience property - most tests only use a single p2p connection to each
        node, so this saves having to write node.p2ps[0] many times."""
        assert self.p2ps, self._node_msg("No p2p connection")
        return self.p2ps[0]

    def disconnect_p2ps(self):
        """Close all p2p connections to the node."""
        for p in self.p2ps:
            p.peer_disconnect()
        del self.p2ps[:]

class TestNodeCLIAttr:
    def __init__(self, cli, command):
        self.cli = cli
        self.command = command

    def __call__(self, *args, **kwargs):
        return self.cli.send_cli(self.command, *args, **kwargs)

    def get_request(self, *args, **kwargs):
        return lambda: self(*args, **kwargs)

class TestNodeCLI():
    """Interface to bitcoin-cli for an individual node"""

    def __init__(self, binary, datadir, chain):
        self.options = []
        self.binary = binary
        self.datadir = datadir
        self.chain = chain
        self.input = None
        self.log = logging.getLogger('TestFramework.bitcoincli')

    def __call__(self, *options, input=None):
        # TestNodeCLI is callable with bitcoin-cli command-line options
        cli = TestNodeCLI(self.binary, self.datadir, self.chain)
        cli.options = [str(o) for o in options]
        cli.input = input
        return cli

    def __getattr__(self, command):
        return TestNodeCLIAttr(self, command)

    def batch(self, requests):
        results = []
        for request in requests:
            try:
                results.append(dict(result=request()))
            except JSONRPCException as e:
                results.append(dict(error=e))
        return results

    def send_cli(self, command=None, *args, **kwargs):
        """Run bitcoin-cli command. Deserializes returned string as python object."""
        pos_args = [str(arg).lower() if type(arg) is bool else str(arg) for arg in args]
        named_args = [str(key) + "=" + str(value) for (key, value) in kwargs.items()]
        assert not (pos_args and named_args), "Cannot use positional arguments and named arguments in the same bitcoin-cli call"
        p_args = [self.binary, "-datadir=" + self.datadir, "-chain=" + self.chain] + self.options
        if named_args:
            p_args += ["-named"]
        if command is not None:
            p_args += [command]
        p_args += pos_args + named_args
        self.log.debug("Running bitcoin-cli command: %s" % command)
        process = subprocess.Popen(p_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        cli_stdout, cli_stderr = process.communicate(input=self.input)
        returncode = process.poll()
        if returncode:
            match = re.match(r'error code: ([-0-9]+)\nerror message:\n(.*)', cli_stderr)
            if match:
                code, message = match.groups()
                raise JSONRPCException(dict(code=int(code), message=message))
            # Ignore cli_stdout, raise with cli_stderr
            raise subprocess.CalledProcessError(returncode, self.binary, output=cli_stderr)
        try:
            return json.loads(cli_stdout, parse_float=decimal.Decimal)
        except JSONDecodeError:
            return cli_stdout.rstrip("\n")
