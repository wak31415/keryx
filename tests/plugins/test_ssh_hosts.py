"""Reading `~/.ssh/config` for the hosts `cluster_stats` could ask. Nothing spawns ssh.

Every config here is synthetic; `ssh -G` and `ssh -O check` are answered by a fake `run`.
"""

import subprocess

from keryx.integrations.cluster import ClusterError
from keryx.plugins import ssh_hosts
from keryx.plugins.ssh_hosts import SshHost


def config(tmp_path, text: str, name: str = "config"):
    path = tmp_path / ".ssh" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def effective(**hosts):
    """A fake `run` answering `ssh -G ALIAS` from `hosts[alias]`, a dict of settings."""
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(argv)
        alias = argv[-1]
        if alias not in hosts:
            return subprocess.CompletedProcess(argv, 255, "", "no such host")
        lines = "\n".join(f"{key} {value}" for key, value in hosts[alias].items())
        return subprocess.CompletedProcess(argv, 0, lines + "\n", "")

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_concrete_aliases_are_read_and_patterns_are_skipped(tmp_path):
    path = config(tmp_path, """
Host alpha alpha-login
    HostName login.alpha.example
Host *.example !beta ?ne
    User nobody
Host=beta
Match host beta
    User someone
""")

    assert ssh_hosts.aliases(path) == ["alpha", "alpha-login", "beta"]


def test_include_is_followed_with_globs_relative_to_the_ssh_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config(tmp_path, "Host gamma\n", name="config.d/one.conf")
    config(tmp_path, "Host delta\n", name="config.d/two.conf")
    path = config(tmp_path, "Include config.d/*.conf\nHost alpha\nInclude config\n")

    assert ssh_hosts.aliases(path) == ["gamma", "delta", "alpha"]


def test_a_missing_config_is_no_hosts(tmp_path):
    assert ssh_hosts.aliases(tmp_path / "nope") == []


def test_ssh_decides_whether_a_host_has_a_control_master(tmp_path):
    path = config(tmp_path, "Host alpha\nHost beta\nHost gamma\n")
    run = effective(
        alpha={"hostname": "login.alpha.example", "user": "me", "controlmaster": "auto",
               "controlpath": "~/.ssh/cm-%C"},
        beta={"hostname": "beta.example", "user": "me", "controlmaster": "no",
              "controlpath": "none"},
    )

    found = ssh_hosts.discover(path, run=run)

    assert found == [
        SshHost("alpha", "login.alpha.example", "me", True, "~/.ssh/cm-%C"),
        SshHost("beta", "beta.example", "me", False, ""),
        SshHost("gamma", "gamma", "", False, ""),
    ]
    assert run.calls[0] == ["ssh", "-F", str(path), "-G", "alpha"]


def test_a_control_master_with_no_socket_path_is_none(tmp_path):
    run = effective(alpha={"controlmaster": "yes", "controlpath": "none"})

    assert ssh_hosts.resolve("alpha", run=run).control_master is False


def test_no_ssh_at_all_is_hosts_without_masters(tmp_path):
    def run(argv, **kwargs):
        raise FileNotFoundError("ssh")

    assert ssh_hosts.resolve("alpha", run=run) == SshHost("alpha", "alpha", "", False, "")


def test_the_default_config_is_resolved_without_naming_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config(tmp_path, "Host alpha\n")
    run = effective(alpha={"controlmaster": "auto", "controlpath": "/tmp/cm"})

    [host] = ssh_hosts.discover(run=run)

    assert host.control_master and run.calls == [["ssh", "-G", "alpha"]]


def test_a_live_master_is_the_local_socket_check_and_nothing_else():
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0 if argv[-1] == "alpha" else 255, "", "")

    assert ssh_hosts.master_alive("alpha", run=run) is True
    assert ssh_hosts.master_alive("beta", run=run) is False
    assert calls == [["ssh", "-O", "check", "alpha"], ["ssh", "-O", "check", "beta"]]


def test_a_check_that_cannot_run_is_no_master():
    def run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5)

    assert ssh_hosts.master_alive("alpha", run=run) is False


class FakeSsh:
    def __init__(self, out="", error=None):
        self.out, self.error, self.asked = out, error, []

    async def run(self, host, script):
        self.asked.append((host, script))
        if self.error:
            raise self.error
        return self.out


def test_partitions_are_read_through_the_guard_and_the_default_is_unmarked():
    ssh = FakeSsh("gpu*\npli\ngpu\n\n")

    assert ssh_hosts.partitions("alpha", ssh=ssh) == ["gpu", "pli"]
    assert ssh.asked == [("alpha", "sinfo -h -o %P")]


def test_partitions_over_a_dead_master_are_none():
    assert ssh_hosts.partitions("alpha", ssh=FakeSsh(error=ClusterError("auth_expired"))) == []
