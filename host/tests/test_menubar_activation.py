"""FocusKeeper: the app that was in front gets the keyboard back (activation.py).

AppKit is replaced by small stand-ins, so this runs on every platform."""

from types import SimpleNamespace

from clawd_tank_menubar.activation import FocusKeeper


class FakeProc:
    def __init__(self, pid, terminated=False):
        self.pid, self.terminated, self.activated_with = pid, terminated, []

    def processIdentifier(self):
        return self.pid

    def isTerminated(self):
        return self.terminated

    def activateWithOptions_(self, options):
        self.activated_with.append(options)


class FakeApp:
    def __init__(self, active=False, modal=None):
        self.active, self.modal, self.calls = active, modal, []

    def isActive(self):
        return self.active

    def modalWindow(self):
        return self.modal

    def activateIgnoringOtherApps_(self, flag):
        self.calls.append(("activate", flag))
        self.active = True

    def hide_(self, sender):
        self.calls.append(("hide",))
        self.active = False


def make(front, active=False, modal=None):
    me = FakeProc(100)
    app = FakeApp(active=active, modal=modal)
    workspace = SimpleNamespace(frontmostApplication=lambda: front)
    running = SimpleNamespace(currentApplication=lambda: me)
    return FocusKeeper(app, workspace, running), app


def test_it_remembers_the_app_in_front_and_activates_itself():
    terminal = FakeProc(7)
    keeper, app = make(terminal)
    keeper.activate()
    assert keeper.remembered is terminal
    assert app.calls == [("activate", True)]


def test_giving_back_activates_that_app_and_forgets_it():
    terminal = FakeProc(7)
    keeper, app = make(terminal)
    keeper.activate()
    keeper.give_back()
    assert terminal.activated_with == [0]
    assert keeper.remembered is None
    assert ("hide",) not in app.calls


def test_the_first_remembered_app_is_kept_while_this_app_stays_active():
    """A dialog that opens as the flyout closes must not remember Clawd Tank itself."""
    terminal, other = FakeProc(7), FakeProc(8)
    keeper, app = make(terminal)
    keeper.activate()
    keeper._workspace_obj = SimpleNamespace(frontmostApplication=lambda: other)
    keeper.activate()                         # already active: nothing is remembered again
    assert keeper.remembered is terminal


def test_this_app_in_front_is_never_remembered():
    keeper, app = make(FakeProc(100))         # the same process id as the app itself
    keeper.activate()
    assert keeper.remembered is None


def test_nothing_is_given_back_when_the_user_went_to_another_app():
    terminal = FakeProc(7)
    keeper, app = make(terminal)
    keeper.activate()
    app.active = False                        # a click on another app's window activated it
    keeper.give_back()
    assert terminal.activated_with == [] and ("hide",) not in app.calls
    assert keeper.remembered is None


def test_nothing_is_given_back_while_a_dialog_is_up_and_the_memory_is_kept_for_its_end():
    terminal = FakeProc(7)
    keeper, app = make(terminal)
    keeper.activate()
    app.modal = object()                      # an NSAlert is running
    keeper.give_back()
    assert terminal.activated_with == [] and keeper.remembered is terminal
    app.modal = None                          # the dialog ended
    keeper.give_back()
    assert terminal.activated_with == [0]


def test_without_a_remembered_app_it_hides_so_macos_picks_the_next_one():
    keeper, app = make(None)
    keeper.activate()
    keeper.give_back()
    assert ("hide",) in app.calls


def test_an_app_that_quit_meanwhile_is_not_activated():
    gone = FakeProc(7, terminated=True)
    keeper, app = make(gone)
    keeper.activate()
    keeper.give_back()
    assert gone.activated_with == [] and ("hide",) in app.calls


def test_a_failing_appkit_call_never_escapes():
    class Angry(FakeProc):
        def activateWithOptions_(self, options):
            raise RuntimeError("boom")
    keeper, app = make(Angry(7))
    keeper.activate()
    keeper.give_back()                        # must not raise


def test_a_workspace_that_cannot_say_leaves_nothing_remembered():
    def broken():
        raise RuntimeError("no workspace")
    me = FakeProc(100)
    keeper = FocusKeeper(FakeApp(), SimpleNamespace(frontmostApplication=broken),
                         SimpleNamespace(currentApplication=lambda: me))
    keeper.activate()
    assert keeper.remembered is None
