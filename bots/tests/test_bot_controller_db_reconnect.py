from unittest.mock import MagicMock, patch

from django.db import InterfaceError, OperationalError
from django.test import SimpleTestCase

from bots.bot_controller import bot_controller as bot_controller_module
from bots.bot_controller.bot_controller import DB_CONNECTION_LOST_GRACE_SECONDS, BotController


def make_controller():
    # Skip __init__: the timeout callback only touches these collaborators.
    controller = BotController.__new__(BotController)
    controller.first_timeout_call = False
    controller.db_connection_lost_at = None
    controller.bot_in_db = MagicMock()
    controller.bot_in_db.last_heartbeat_timestamp = None
    controller.per_participant_non_streaming_audio_input_manager = MagicMock()
    controller.screen_and_audio_recorder = None
    controller.audio_chunk_uploader = None
    controller.per_participant_streaming_audio_input_manager = MagicMock()
    controller.closed_caption_manager = MagicMock()
    controller.adapter = MagicMock()
    controller.audio_output_manager = MagicMock()
    controller.video_output_manager = MagicMock()
    controller.join_if_staged_and_time_to_join = MagicMock()
    controller.bot_resource_snapshot_taker = MagicMock()
    controller.handle_exception_in_timeout_callback = MagicMock()
    return controller


@patch.object(bot_controller_module, "close_old_connections")
@patch.object(bot_controller_module, "connection")
class BotControllerDbReconnectTest(SimpleTestCase):
    def test_dropped_connection_reconnects_instead_of_fatal_error(self, mock_connection, _):
        controller = make_controller()
        process_chunks = controller.per_participant_non_streaming_audio_input_manager.process_chunks
        process_chunks.side_effect = [OperationalError("server closed the connection unexpectedly"), None]

        self.assertTrue(controller.on_main_loop_timeout())
        mock_connection.close.assert_called_once()
        controller.handle_exception_in_timeout_callback.assert_not_called()

        # The next tick succeeds and clears the outage.
        self.assertTrue(controller.on_main_loop_timeout())
        self.assertIsNone(controller.db_connection_lost_at)
        controller.handle_exception_in_timeout_callback.assert_not_called()

    def test_gives_up_after_grace_period(self, mock_connection, _):
        controller = make_controller()
        controller.per_participant_non_streaming_audio_input_manager.process_chunks.side_effect = InterfaceError("connection already closed")

        with patch.object(bot_controller_module.time, "time", return_value=1000.0):
            self.assertTrue(controller.on_main_loop_timeout())
        with patch.object(bot_controller_module.time, "time", return_value=1000.0 + DB_CONNECTION_LOST_GRACE_SECONDS + 1):
            self.assertFalse(controller.on_main_loop_timeout())

        controller.handle_exception_in_timeout_callback.assert_called_once()

    def test_other_errors_are_still_fatal(self, mock_connection, _):
        controller = make_controller()
        controller.per_participant_non_streaming_audio_input_manager.process_chunks.side_effect = ValueError("boom")

        self.assertFalse(controller.on_main_loop_timeout())
        controller.handle_exception_in_timeout_callback.assert_called_once()
        mock_connection.close.assert_not_called()


@patch.object(bot_controller_module, "connection")
class BotControllerStartupAndCleanupDbReconnectTest(SimpleTestCase):
    def test_startup_retries_a_dropped_connection(self, mock_connection):
        bot = MagicMock()
        with patch.object(bot_controller_module.Bot.objects, "get", side_effect=[OperationalError("server closed the connection unexpectedly"), bot]) as mock_get, patch.object(bot_controller_module.time, "sleep"):
            self.assertIs(BotController.get_bot_with_db_retry(123), bot)
        self.assertEqual(mock_get.call_count, 2)
        mock_connection.close.assert_called_once()

    def test_startup_gives_up_after_grace_period(self, mock_connection):
        times = iter([1000.0, 1000.0 + DB_CONNECTION_LOST_GRACE_SECONDS + 1])
        with patch.object(bot_controller_module.Bot.objects, "get", side_effect=OperationalError("down")), patch.object(bot_controller_module.time, "sleep"), patch.object(bot_controller_module.time, "time", side_effect=lambda: next(times)):
            with self.assertRaises(OperationalError):
                BotController.get_bot_with_db_retry(123)

    def test_reset_db_connection_closes_outside_a_transaction(self, mock_connection):
        mock_connection.in_atomic_block = False
        BotController.__new__(BotController).reset_db_connection("test")
        mock_connection.close.assert_called_once()

    def test_reset_db_connection_skips_inside_a_transaction(self, mock_connection):
        mock_connection.in_atomic_block = True
        BotController.__new__(BotController).reset_db_connection("test")
        mock_connection.close.assert_not_called()
