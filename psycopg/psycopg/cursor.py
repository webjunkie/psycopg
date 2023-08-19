"""
Psycopg Cursor object
"""

# Copyright (C) 2020 The Psycopg Team

from types import TracebackType
from typing import Any, Iterable, Iterator, List, Optional, Type, TypeVar
from typing import overload, TYPE_CHECKING
from contextlib import contextmanager

from . import pq
from . import errors as e
from .abc import Query, Params
from .copy import Copy, Writer as CopyWriter
from .rows import Row, RowMaker, RowFactory
from ._pipeline import Pipeline
from ._cursor_base import BaseCursor

if TYPE_CHECKING:
    from .connection import Connection

ACTIVE = pq.TransactionStatus.ACTIVE


class Cursor(BaseCursor["Connection[Any]", Row]):
    __module__ = "psycopg"
    __slots__ = ()
    _Self = TypeVar("_Self", bound="Cursor[Any]")

    @overload
    def __init__(self: "Cursor[Row]", connection: "Connection[Row]"):
        ...

    @overload
    def __init__(
        self: "Cursor[Row]",
        connection: "Connection[Any]",
        *,
        row_factory: RowFactory[Row],
    ):
        ...

    def __init__(
        self,
        connection: "Connection[Any]",
        *,
        row_factory: Optional[RowFactory[Row]] = None,
    ):
        super().__init__(connection)
        self._row_factory = row_factory or connection.row_factory

    def __enter__(self: _Self) -> _Self:
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self.close()

    def close(self) -> None:
        """
        Close the current cursor and free associated resources.
        """
        self._close()

    @property
    def row_factory(self) -> RowFactory[Row]:
        """Writable attribute to control how result rows are formed."""
        return self._row_factory

    @row_factory.setter
    def row_factory(self, row_factory: RowFactory[Row]) -> None:
        self._row_factory = row_factory
        if self.pgresult:
            self._make_row = row_factory(self)

    def _make_row_maker(self) -> RowMaker[Row]:
        return self._row_factory(self)

    def execute(
        self: _Self,
        query: Query,
        params: Optional[Params] = None,
        *,
        prepare: Optional[bool] = None,
        binary: Optional[bool] = None,
    ) -> _Self:
        """
        Execute a query or command to the database.
        """
        try:
            with self._conn.lock:
                self._conn.wait(
                    self._execute_gen(query, params, prepare=prepare, binary=binary)
                )
        except e._NO_TRACEBACK as ex:
            raise ex.with_traceback(None)
        return self

    def executemany(
        self,
        query: Query,
        params_seq: Iterable[Params],
        *,
        returning: bool = False,
    ) -> None:
        """
        Execute the same command with a sequence of input data.
        """
        try:
            if Pipeline.is_supported():
                # If there is already a pipeline, ride it, in order to avoid
                # sending unnecessary Sync.
                with self._conn.lock:
                    p = self._conn._pipeline
                    if p:
                        self._conn.wait(
                            self._executemany_gen_pipeline(query, params_seq, returning)
                        )
                # Otherwise, make a new one
                if not p:
                    with self._conn.pipeline(), self._conn.lock:
                        self._conn.wait(
                            self._executemany_gen_pipeline(query, params_seq, returning)
                        )
            else:
                with self._conn.lock:
                    self._conn.wait(
                        self._executemany_gen_no_pipeline(query, params_seq, returning)
                    )
        except e._NO_TRACEBACK as ex:
            raise ex.with_traceback(None)

    def stream(
        self,
        query: Query,
        params: Optional[Params] = None,
        *,
        binary: Optional[bool] = None,
    ) -> Iterator[Row]:
        """
        Iterate row-by-row on a result from the database.
        """
        if self._pgconn.pipeline_status:
            raise e.ProgrammingError("stream() cannot be used in pipeline mode")

        with self._conn.lock:
            try:
                self._conn.wait(self._stream_send_gen(query, params, binary=binary))
                first = True
                while self._conn.wait(self._stream_fetchone_gen(first)):
                    # We know that, if we got a result, it has a single row.
                    rec: Row = self._tx.load_row(0, self._make_row)  # type: ignore
                    yield rec
                    first = False

            except e._NO_TRACEBACK as ex:
                raise ex.with_traceback(None)

            finally:
                if self._pgconn.transaction_status == ACTIVE:
                    # Try to cancel the query, then consume the results
                    # already received.
                    self._conn.cancel()
                    try:
                        while self._conn.wait(self._stream_fetchone_gen(first=False)):
                            pass
                    except Exception:
                        pass

                    # Try to get out of ACTIVE state. Just do a single attempt, which
                    # should work to recover from an error or query cancelled.
                    try:
                        self._conn.wait(self._stream_fetchone_gen(first=False))
                    except Exception:
                        pass

    def fetchone(self) -> Optional[Row]:
        """
        Return the next record from the current recordset.

        Return `!None` the recordset is finished.

        :rtype: Optional[Row], with Row defined by `row_factory`
        """
        self._fetch_pipeline()
        self._check_result_for_fetch()
        record = self._tx.load_row(self._pos, self._make_row)
        if record is not None:
            self._pos += 1
        return record

    def fetchmany(self, size: int = 0) -> List[Row]:
        """
        Return the next `!size` records from the current recordset.

        `!size` default to `!self.arraysize` if not specified.

        :rtype: Sequence[Row], with Row defined by `row_factory`
        """
        self._fetch_pipeline()
        self._check_result_for_fetch()
        assert self.pgresult

        if not size:
            size = self.arraysize
        records = self._tx.load_rows(
            self._pos,
            min(self._pos + size, self.pgresult.ntuples),
            self._make_row,
        )
        self._pos += len(records)
        return records

    def fetchall(self) -> List[Row]:
        """
        Return all the remaining records from the current recordset.

        :rtype: Sequence[Row], with Row defined by `row_factory`
        """
        self._fetch_pipeline()
        self._check_result_for_fetch()
        assert self.pgresult
        records = self._tx.load_rows(self._pos, self.pgresult.ntuples, self._make_row)
        self._pos = self.pgresult.ntuples
        return records

    def __iter__(self) -> Iterator[Row]:
        self._fetch_pipeline()
        self._check_result_for_fetch()

        def load(pos: int) -> Optional[Row]:
            return self._tx.load_row(pos, self._make_row)

        while True:
            row = load(self._pos)
            if row is None:
                break
            self._pos += 1
            yield row

    def scroll(self, value: int, mode: str = "relative") -> None:
        """
        Move the cursor in the result set to a new position according to mode.

        If `!mode` is ``'relative'`` (default), `!value` is taken as offset to
        the current position in the result set; if set to ``'absolute'``,
        `!value` states an absolute target position.

        Raise `!IndexError` in case a scroll operation would leave the result
        set. In this case the position will not change.
        """
        self._fetch_pipeline()
        self._scroll(value, mode)

    @contextmanager
    def copy(
        self,
        statement: Query,
        params: Optional[Params] = None,
        *,
        writer: Optional[CopyWriter] = None,
    ) -> Iterator[Copy]:
        """
        Initiate a :sql:`COPY` operation and return an object to manage it.

        :rtype: Copy
        """
        try:
            with self._conn.lock:
                self._conn.wait(self._start_copy_gen(statement, params))

            with Copy(self, writer=writer) as copy:
                yield copy
        except e._NO_TRACEBACK as ex:
            raise ex.with_traceback(None)

        # If a fresher result has been set on the cursor by the Copy object,
        # read its properties (especially rowcount).
        self._select_current_result(0)

    def _fetch_pipeline(self) -> None:
        if (
            self._execmany_returning is not False
            and not self.pgresult
            and self._conn._pipeline
        ):
            with self._conn.lock:
                self._conn.wait(self._conn._pipeline._fetch_gen(flush=True))
