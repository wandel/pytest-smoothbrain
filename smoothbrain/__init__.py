import os
import glob
import logging
import tempfile

import pytest

import vix


def setup():
    filename = os.getenv("PYTEST_XDIST_WORKER", "master")
    hdlr = logging.FileHandler(filename + ".log", "w")
    hdlr.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(hdlr)


setup()


class Machine:
    count: int = 0
    initialized: bool = False

    def acquire(self):
        self.count += 1

    def release(self):
        self.count -= 1
        if self.count == 0:
            self.teardown()

    def setup(self):
        raise NotImplementedError

    def teardown(self):
        raise NotImplementedError

    def reset(self):
        raise NotImplementedError

    def power_on(self):
        raise NotImplementedError

    def power_off(self):
        raise NotImplementedError

    def upload(self, path: str, data: bytes):
        raise NotImplementedError

    def download(self, path: str) -> bytes:
        raise NotImplementedError

    def execute(self, *args, **kwargs):
        raise NotImplementedError


class VixMachine(Machine):
    path: str = None
    host: vix.VixHost = None
    template: vix.VixVM = None
    snapshot: vix.VixSnapshot = None

    @property
    def name(self) -> str:
        return self.template.name

    def __init__(self, path, snapshot="initial"):
        print("opening: path=%s, snapshot=%s" % (path, snapshot))
        self.host = vix.VixHost()
        self.template = self.host.open_vm(path)
        if snapshot:
            self.snapshot = self.template.snapshot_get_named(snapshot)

    def setup(self):
        logging.info("%s: setup", self.name)
        if not self.template.is_running:
            self.template.power_on(launch_gui=True)
        self.template.wait_for_tools(timeout=60)

    def reset(self):
        logging.info("%s: reset", self.name)
        if self.snapshot:
            self.template.snapshot_revert(self.snapshot)

        if not self.template.is_running:
            self.template.power_on(launch_gui=True)
        self.template.wait_for_tools(timeout=60)

    def teardown(self):
        logging.info("%s: teardown", self.name)
        if self.snapshot:
            self.template.snapshot_revert(self.snapshot)
        # if self.template.is_running:
            # self.template.power_off(from_guest=True)

    def upload(self, path: str, data: bytes):
        parent = os.path.dirname(path)
        if not self.template.dir_exists(parent):
            self.template.create_directory(parent)

        tmp = None
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            tmp = f.name
        try:
            self.template.copy_host_to_guest(tmp, path)
        finally:
            os.unlink(tmp)

    def download(self, path: str) -> bytes:
        tmp = None
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp = f.name

        try:
            self.template.copy_guest_to_host(path, tmp)
            with open(tmp, 'rb') as f:
                return f.read()
        finally:
            os.unlink(tmp)

    def execute(self, path, *args, stdin: str | bytes = None, wait: bool = True):
        self.template.proc_run(path, args, should_block=wait)

    def login(self, username: str, password: str):
        return self.template.login(username, password)


machines = []


def pytest_addoption(parser: pytest.Parser):
    parser.addoption("--pattern", help="glob to select the different platforms")
    parser.addoption("--snapshot", help="name of the snapshot to use")


def pytest_sessionstart(session: pytest.Session):
    snapshot = session.config.getoption("--snapshot")
    pattern = session.config.getoption("--pattern")
    if not pattern:
        pytest.exit("--pattern is required")

    global machines
    for path in glob.glob(pattern, recursive=True):
        machine = VixMachine(path, snapshot)
        machines.append(machine)
    if len(machines) == 0:
        pytest.exit("no machines found")


def pytest_generate_tests(metafunc: pytest.Metafunc):
    print("generate:", metafunc.definition.name, metafunc.fixturenames)
    if "target" not in metafunc.fixturenames:
        return

    params = []
    for machine in machines:
        machine.acquire()
        marks = [pytest.mark.xdist_group(machine.name)]
        params.append(pytest.param(machine, marks=marks, id=machine.name))
    metafunc.parametrize("target", params)


def pytest_runtest_call(item: pytest.Item):
    if "target" not in item.funcargs:
        return

    machine = item.funcargs["target"]
    if not machine.initialized:
        machine.setup()
        machine.initialized = True
    machine.reset()


def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item):
    if "target" not in item.funcargs:
        return

    machine = item.funcargs["target"]
    machine.release()
