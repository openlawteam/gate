"""Async Textual integration tests for gate.tui.

Follows Hopper's pattern (``App.run_test()`` + ``Pilot``) to validate that
the TUI actually renders and reacts correctly. Pure helper tests remain in
``test_tui_unit.py``.
"""


from gate.tui import GateTUI


class MockServer:
    """Minimal stand-in for GateServer that the TUI can read from.

    The TUI only reads ``server.reviews``, ``server.review_queue``, and
    ``server.health``, plus calls ``server.enqueue`` for cancel actions.
    """

    def __init__(
        self,
        reviews=None,
        review_queue=None,
        health=None,
        tmux_location=None,
    ):
        self.reviews = reviews if reviews is not None else []
        self.review_queue = review_queue if review_queue is not None else []
        self.health = health if health is not None else {}
        self.tmux_location = tmux_location
        self.enqueued: list[dict] = []
        self.started_at = 0

    def enqueue(self, message: dict) -> None:
        self.enqueued.append(message)


# ── App lifecycle ────────────────────────────────────────────


class TestAppLifecycle:
    async def test_app_starts_with_no_server(self):
        app = GateTUI()
        async with app.run_test() as pilot:
            assert app.title == "gate"
            await pilot.pause()

    async def test_app_starts_with_empty_server(self):
        server = MockServer()
        app = GateTUI(server=server)
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable
            reviews_table = app.query_one("#reviews-table", DataTable)
            queue_table = app.query_one("#queue-table", DataTable)
            recent_table = app.query_one("#recent-table", DataTable)
            assert reviews_table is not None
            assert queue_table is not None
            assert recent_table is not None

    async def test_app_renders_title_header(self):
        app = GateTUI()
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import Header
            headers = app.query(Header)
            assert len(list(headers)) > 0


# ── Table structure ──────────────────────────────────────────


class TestTableColumns:
    async def test_reviews_table_has_expected_columns(self):
        server = MockServer()
        app = GateTUI(server=server)
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable
            table = app.query_one("#reviews-table", DataTable)
            # 7 columns: icon, Repo, PR, Stage, Pipeline, Status, Elapsed
            assert len(table.columns) == 7

    async def test_queue_table_has_three_columns(self):
        server = MockServer()
        app = GateTUI(server=server)
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable
            table = app.query_one("#queue-table", DataTable)
            assert len(table.columns) == 3

    async def test_recent_table_has_six_columns(self):
        server = MockServer()
        app = GateTUI(server=server)
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable
            table = app.query_one("#recent-table", DataTable)
            assert len(table.columns) == 6


# ── Review rendering ─────────────────────────────────────────


class TestReviewRendering:
    async def test_active_review_appears_in_table(self):
        server = MockServer(reviews=[
            {
                "id": "a-b-pr42", "repo": "a/b", "pr_number": 42,
                "stage": "triage", "status": "running",
                "started_at": 0, "updated_at": 0,
            }
        ])
        app = GateTUI(server=server)
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable
            table = app.query_one("#reviews-table", DataTable)
            # _refresh_tables runs in on_mount and polling; poll once manually
            app._refresh_reviews_table()
            await pilot.pause()
            assert table.row_count >= 1

    async def test_multiple_reviews_render(self):
        server = MockServer(reviews=[
            {"id": f"r{i}", "repo": "a/b", "pr_number": i,
             "stage": "triage", "status": "running",
             "started_at": 0, "updated_at": 0}
            for i in range(3)
        ])
        app = GateTUI(server=server)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._refresh_reviews_table()
            await pilot.pause()
            from textual.widgets import DataTable
            table = app.query_one("#reviews-table", DataTable)
            assert table.row_count == 3


# ── Queue rendering ──────────────────────────────────────────


class TestQueueRendering:
    async def test_queue_items_appear(self):
        server = MockServer(review_queue=[
            {"pr_number": 1, "repo": "a/b"},
            {"pr_number": 2, "repo": "a/b"},
        ])
        app = GateTUI(server=server)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._refresh_queue_table()
            await pilot.pause()
            from textual.widgets import DataTable
            table = app.query_one("#queue-table", DataTable)
            assert table.row_count == 2

    async def test_empty_queue_shows_no_rows(self):
        server = MockServer(review_queue=[])
        app = GateTUI(server=server)
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable
            table = app.query_one("#queue-table", DataTable)
            assert table.row_count == 0


# ── Health panel ─────────────────────────────────────────────


class TestHealthPanel:
    async def test_health_ok_renders(self):
        server = MockServer(health={"ok": True, "checks": {"disk": {"ok": True}}})
        app = GateTUI(server=server)
        async with app.run_test() as pilot:
            await pilot.pause()
            # Force the health refresh
            app._refresh_health()
            await pilot.pause()
            content = app.query_one("#health-content")
            # Static renders the checks
            assert content is not None

    async def test_no_health_data_is_safe(self):
        server = MockServer(health={})
        app = GateTUI(server=server)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._refresh_health()
            await pilot.pause()


# ── Dynamic updates ──────────────────────────────────────────


class TestDynamicUpdates:
    async def test_adding_review_updates_table(self):
        server = MockServer()
        app = GateTUI(server=server)
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable
            table = app.query_one("#reviews-table", DataTable)
            initial = table.row_count
            # Now mutate in-place
            server.reviews.append({
                "id": "new", "repo": "a/b", "pr_number": 99,
                "stage": "triage", "status": "running",
                "started_at": 0, "updated_at": 0,
            })
            app._refresh_reviews_table()
            await pilot.pause()
            assert table.row_count == initial + 1

    async def test_removing_review_updates_table(self):
        server = MockServer(reviews=[
            {"id": "gone", "repo": "a/b", "pr_number": 1,
             "stage": "triage", "status": "running",
             "started_at": 0, "updated_at": 0}
        ])
        app = GateTUI(server=server)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._refresh_reviews_table()
            await pilot.pause()
            from textual.widgets import DataTable
            table = app.query_one("#reviews-table", DataTable)
            assert table.row_count == 1
            server.reviews.clear()
            app._refresh_reviews_table()
            await pilot.pause()
            assert table.row_count == 0


# ── Keyboard bindings ────────────────────────────────────────


class TestKeyboardBindings:
    async def test_quit_key_stops_app(self):
        app = GateTUI()
        async with app.run_test() as pilot:
            await pilot.press("q")
            # After q the app should be exiting; we just verify no crash


# ── Server interaction via enqueue ───────────────────────────


class TestServerEnqueue:
    async def test_server_reference_is_kept(self):
        server = MockServer()
        app = GateTUI(server=server)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.server is server
