"""The three facts a request carries, and the task-locality that keeps them apart.

`settings_api.core.context` holds the request id, the authenticated account and -- on the
service-facing surface -- the calling service, so that a log record or an error body can
name them without every signature in the service threading them through. That convenience
is only safe because of one property, and this file exists to pin it:

**A binding belongs to the task that made it and to nothing else.** One worker serves many
requests, and the account is bound by a FastAPI dependency that has no matching unbind
(:func:`~settings_api.core.context.set_account_id` says so in its own docstring). If a
value set while serving one person could be read while serving the next, this module would
be a cross-account data leak with a helpful API -- so the concurrency tests below force two
tasks to interleave, each writing before the other reads, and assert that neither sees the
other's value. Without the forced interleaving both tasks would read whatever was written
last and the test would pass under a plain global.

The rest is the contract that makes reading a binding meaningful: the getters answer
``None`` outside a request rather than the last request's value, a ``bind_*`` block
restores exactly what it displaced -- including when the block raises -- and binding one of
the three facts never disturbs the other two.
"""

from __future__ import annotations

import asyncio
import contextvars
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import TypeVar

import pytest

from settings_api.core.context import (
    bind_account_id,
    bind_request_id,
    bind_service,
    get_account_id,
    get_request_id,
    get_service,
    new_request_id,
    set_account_id,
    set_service,
)

T = TypeVar("T")

Reader = Callable[[], "str | None"]
Binder = Callable[[str], AbstractContextManager[str]]
Setter = Callable[[str], None]

BINDERS: list[tuple[Binder, Reader]] = [
    (bind_request_id, get_request_id),
    (bind_account_id, get_account_id),
    (bind_service, get_service),
]
BINDER_IDS = ["request_id", "account_id", "service"]

SETTERS: list[tuple[Setter, Binder, Reader]] = [
    (set_account_id, bind_account_id, get_account_id),
    (set_service, bind_service, get_service),
]
SETTER_IDS = ["account_id", "service"]

READERS: list[Reader] = [get_request_id, get_account_id, get_service]


class _TheBlockFailedError(Exception):
    """Raised inside a bound block, to prove the binding is unwound anyway."""


def in_a_fresh_context[T](work: Callable[[], T]) -> T:
    """Run ``work`` with every context variable back at its default.

    A synchronous test that calls a ``set_*`` form has no way to unbind it -- that is the
    whole point of those functions -- so running such a test in the ambient context would
    leave the value behind for whatever test pytest collects next. An empty
    :class:`contextvars.Context` starts from the declared defaults rather than from a copy
    of the current one, which also means the "outside a request" assertions below cannot be
    made to pass or fail by test ordering.
    """
    return contextvars.Context().run(work)


async def _write_then_read_once_the_other_task_has_written(
    write: Setter, read: Reader, value: str, mine: asyncio.Event, theirs: asyncio.Event
) -> str | None:
    """Bind ``value``, wait until the sibling task has bound its own, then read back."""
    write(value)
    mine.set()
    await theirs.wait()
    return read()


async def _bind_then_read_once_the_other_task_has_bound(
    bind: Binder, read: Reader, value: str, mine: asyncio.Event, theirs: asyncio.Event
) -> str | None:
    """The same interleaving, for the block form."""
    with bind(value):
        mine.set()
        await theirs.wait()
        return read()


class TestOutsideARequest:
    """Nothing is bound until the edge binds it, and the getters have to say so."""

    @pytest.mark.parametrize("read", READERS, ids=BINDER_IDS)
    def test_a_fact_that_was_never_bound_reads_as_none(self, read: Reader) -> None:
        # `None` rather than "" or a sentinel: `core/logging.py` drops these fields from a
        # record when they are None, so a getter that invented a value would put a
        # meaningless account id on every startup and shutdown log line.
        assert in_a_fresh_context(read) is None

    @pytest.mark.parametrize(("bind", "read"), BINDERS, ids=BINDER_IDS)
    def test_a_fact_reads_as_none_again_once_its_block_has_ended(
        self, bind: Binder, read: Reader
    ) -> None:
        def bind_and_leave() -> str | None:
            with bind("bound-for-a-moment"):
                pass
            return read()

        # The failure this prevents is the nastiest one here: a request whose id outlives
        # its own block would stamp the *next* request's log records with itself.
        assert in_a_fresh_context(bind_and_leave) is None


class TestRequestIds:
    """A request id is generated here and nowhere else, and it has to be unrepeatable."""

    def test_each_request_id_is_a_fresh_random_uuid4(self) -> None:
        request_id = new_request_id()

        # Version 4 specifically: the id reaches clients in the `X-Request-Id` header and
        # in every problem+json body, so a sequential or time-derived id would leak how
        # much traffic the service is serving and when.
        assert uuid.UUID(hex=request_id).version == 4

    def test_request_ids_do_not_repeat(self) -> None:
        minted = [new_request_id() for _ in range(1_000)]

        assert len(set(minted)) == len(minted)

    def test_a_request_id_is_lowercase_hex_with_no_punctuation(self) -> None:
        request_id = new_request_id()

        # The bare hex form, not the dashed one: it goes out in the `X-Request-Id` header
        # and into log fields, so switching spellings would silently break every consumer
        # that ever matched one.
        assert len(request_id) == 32
        assert set(request_id) <= set("0123456789abcdef")

    def test_the_id_that_was_bound_is_the_id_that_is_read(self) -> None:
        request_id = new_request_id()

        with bind_request_id(request_id) as bound:
            assert bound == request_id
            assert get_request_id() == request_id


class TestABindingUnwinds:
    """`bind_*` promises to restore what it displaced. Every path out of the block."""

    @pytest.mark.parametrize(("bind", "read"), BINDERS, ids=BINDER_IDS)
    def test_a_block_restores_the_value_it_displaced(self, bind: Binder, read: Reader) -> None:
        def outer_then_inner() -> tuple[str | None, str | None]:
            with bind("outer"):
                with bind("inner"):
                    inner = read()
                return inner, read()

        inner, after_inner = in_a_fresh_context(outer_then_inner)

        assert inner == "inner"
        # Restored to "outer", not cleared to None. The two are easy to confuse in an
        # implementation that unbinds by setting the default, and the difference shows up
        # only where bindings nest -- which is exactly where a wrong answer is expensive.
        assert after_inner == "outer"

    @pytest.mark.parametrize(("bind", "read"), BINDERS, ids=BINDER_IDS)
    def test_a_block_that_raises_still_restores_what_it_displaced(
        self, bind: Binder, read: Reader
    ) -> None:
        def outer_then_failing_inner() -> str | None:
            with bind("outer"):
                with pytest.raises(_TheBlockFailedError), bind("inner"):
                    raise _TheBlockFailedError
                return read()

        # The error path is the one that matters: an unhandled exception inside a request
        # is precisely when the error handler goes looking for the request id, and a
        # binding that only unwound on the happy path would mislabel every later record.
        assert in_a_fresh_context(outer_then_failing_inner) == "outer"

    @pytest.mark.parametrize(("bind", "read"), BINDERS, ids=BINDER_IDS)
    def test_the_exception_from_the_block_is_not_swallowed(
        self, bind: Binder, read: Reader
    ) -> None:
        def bind_and_fail() -> None:
            with bind("bound"):
                raise _TheBlockFailedError

        # A context manager that returned a truthy value from __exit__ would turn every
        # failed request into a silent success. Cheap to write by accident, invisible
        # until production.
        with pytest.raises(_TheBlockFailedError):
            in_a_fresh_context(bind_and_fail)
        assert in_a_fresh_context(read) is None


class TestTheThreeFactsAreIndependent:
    """One `ContextVar` each: binding the account must not move the request id."""

    @pytest.mark.parametrize(("bind", "read"), BINDERS, ids=BINDER_IDS)
    def test_binding_one_fact_leaves_the_other_two_unbound(
        self, bind: Binder, read: Reader
    ) -> None:
        def bind_one() -> list[str | None]:
            with bind("the-one-that-was-bound"):
                return [other() for other in READERS if other is not read]

        # Three variables sharing one storage slot would still pass every single-fact test
        # in this file; only reading the neighbours catches it.
        assert in_a_fresh_context(bind_one) == [None, None]

    @pytest.mark.parametrize(("write", "bind", "read"), SETTERS, ids=SETTER_IDS)
    def test_setting_one_fact_leaves_the_other_two_unbound(
        self, write: Setter, bind: Binder, read: Reader
    ) -> None:
        def set_one() -> list[str | None]:
            write("the-one-that-was-set")
            return [other() for other in READERS if other is not read]

        # The set forms write the same three variables the bind forms do, so they need the
        # same check: an account bound onto the service's variable would put the account id
        # in the `service` log field of every request.
        assert in_a_fresh_context(set_one) == [None, None]

    def test_all_three_can_be_bound_at_once(self) -> None:
        def bind_all() -> tuple[str | None, str | None, str | None]:
            with (
                bind_request_id("req-1"),
                bind_account_id("account-a"),
                bind_service("spotify-api"),
            ):
                return get_request_id(), get_account_id(), get_service()

        assert in_a_fresh_context(bind_all) == ("req-1", "account-a", "spotify-api")


class TestTheSetForms:
    """`set_*` has no unbind on purpose: a dependency binds, and the handler reads."""

    @pytest.mark.parametrize(("write", "bind", "read"), SETTERS, ids=SETTER_IDS)
    def test_a_value_set_in_a_called_function_is_visible_to_its_caller(
        self, write: Setter, bind: Binder, read: Reader
    ) -> None:
        def a_dependency() -> None:
            write("bound-by-the-dependency")

        def a_request() -> str | None:
            a_dependency()
            return read()

        # This is the difference from `bind_*`, stated as behaviour. A FastAPI dependency
        # returns before the handler runs, so a context manager could not span the two --
        # the binding has to outlive the frame that made it.
        assert in_a_fresh_context(a_request) == "bound-by-the-dependency"

    @pytest.mark.parametrize(("write", "bind", "read"), SETTERS, ids=SETTER_IDS)
    def test_setting_a_value_replaces_an_earlier_one(
        self, write: Setter, bind: Binder, read: Reader
    ) -> None:
        def set_twice() -> str | None:
            write("first")
            write("second")
            return read()

        assert in_a_fresh_context(set_twice) == "second"

    @pytest.mark.parametrize(("write", "bind", "read"), SETTERS, ids=SETTER_IDS)
    def test_a_value_set_inside_a_block_is_undone_when_the_block_ends(
        self, write: Setter, bind: Binder, read: Reader
    ) -> None:
        def set_inside_a_block() -> str | None:
            with bind("bound-by-the-block"):
                write("set-inside")
            return read()

        # Belt and braces on the unwind: `reset(token)` restores the value the token
        # recorded even though the variable has been written since, so a `set_*` inside a
        # request cannot outlive the request's own binding.
        assert in_a_fresh_context(set_inside_a_block) is None


class TestTwoRequestsAtOnce:
    """The isolation story. Everything else in this module is a convenience; this is not.

    Each test forces the interleaving rather than hoping for it: both tasks write, both
    tasks wait until the other has written, and only then do they read. Under a plain
    module-level global both would read whichever value landed second, so a passing run
    here is evidence about task-locality rather than about scheduling luck.
    """

    @pytest.mark.parametrize(("write", "bind", "read"), SETTERS, ids=SETTER_IDS)
    async def test_two_concurrent_tasks_never_see_each_others_value(
        self, write: Setter, bind: Binder, read: Reader
    ) -> None:
        first_has_written, second_has_written = asyncio.Event(), asyncio.Event()

        seen_by_first, seen_by_second = await asyncio.gather(
            _write_then_read_once_the_other_task_has_written(
                write, read, "account-a", first_has_written, second_has_written
            ),
            _write_then_read_once_the_other_task_has_written(
                write, read, "account-b", second_has_written, first_has_written
            ),
        )

        # Two people's requests served by one worker at the same moment. If this ever
        # fails, one of them is reading the other's identity.
        assert (seen_by_first, seen_by_second) == ("account-a", "account-b")

    @pytest.mark.parametrize(("bind", "read"), BINDERS, ids=BINDER_IDS)
    async def test_two_concurrent_blocks_never_see_each_others_value(
        self, bind: Binder, read: Reader
    ) -> None:
        first_has_bound, second_has_bound = asyncio.Event(), asyncio.Event()

        seen_by_first, seen_by_second = await asyncio.gather(
            _bind_then_read_once_the_other_task_has_bound(
                bind, read, "first", first_has_bound, second_has_bound
            ),
            _bind_then_read_once_the_other_task_has_bound(
                bind, read, "second", second_has_bound, first_has_bound
            ),
        )

        assert (seen_by_first, seen_by_second) == ("first", "second")

    @pytest.mark.parametrize(("write", "bind", "read"), SETTERS, ids=SETTER_IDS)
    async def test_a_value_set_in_a_child_task_does_not_reach_the_parent(
        self, write: Setter, bind: Binder, read: Reader
    ) -> None:
        async def set_its_own() -> str | None:
            write("belongs-to-the-child")
            return read()

        with bind("belongs-to-the-parent"):
            child = await asyncio.create_task(set_its_own())
            after = read()

        assert child == "belongs-to-the-child"
        # The claim `set_account_id` makes in its own docstring: a binding with no unbind
        # is safe because it dies with the task. A worker that outlived its request's task
        # would otherwise carry that account into the next request it served.
        assert after == "belongs-to-the-parent"

    @pytest.mark.parametrize(("bind", "read"), BINDERS, ids=BINDER_IDS)
    async def test_a_task_started_inside_a_block_inherits_the_binding(
        self, bind: Binder, read: Reader
    ) -> None:
        async def read_what_it_inherited() -> str | None:
            return read()

        with bind("bound-before-the-task-started"):
            inherited = await asyncio.create_task(read_what_it_inherited())

        # The other half of task-locality, and the half the middleware depends on: a task
        # copies the context at creation, so everything a handler spawns is still labelled
        # with the request that spawned it.
        assert inherited == "bound-before-the-task-started"

    async def test_the_three_facts_stay_matched_up_across_concurrent_requests(self) -> None:
        async def serve(
            request_id: str,
            account_id: str,
            service: str,
            mine: asyncio.Event,
            theirs: asyncio.Event,
        ) -> tuple[str | None, ...]:
            with bind_request_id(request_id):
                set_account_id(account_id)
                set_service(service)
                # Hand control to the sibling request between the writes and the reads,
                # rather than hoping the scheduler interleaves them on its own.
                mine.set()
                await theirs.wait()
                return get_request_id(), get_account_id(), get_service()

        first_is_bound, second_is_bound = asyncio.Event(), asyncio.Event()
        first, second = await asyncio.gather(
            serve("req-1", "account-a", "spotify-api", first_is_bound, second_is_bound),
            serve("req-2", "account-b", "downstream-tool", second_is_bound, first_is_bound),
        )

        # Not just "no leak" but "no mix-up": a log record naming request req-1 alongside
        # account-b would be worse than one naming neither.
        assert first == ("req-1", "account-a", "spotify-api")
        assert second == ("req-2", "account-b", "downstream-tool")
