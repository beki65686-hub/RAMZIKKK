import asyncio
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import telegram_bot
from telegram_bot import build_broadcast_message


class BroadcastMessageTests(unittest.TestCase):
    def test_uses_user_message_as_payload(self):
        result = build_broadcast_message(123456, "+998901234567", "Salom dostlar", "ali_v")
        self.assertEqual(
            result,
            "Salom dostlar\n\nTelefon: +998901234567\nFoydalanuvchi: @ali_v",
        )

    def test_broadcast_message_adds_plus_to_phone_without_prefix(self):
        result = build_broadcast_message(123456, "998 90 123 45 67", "Salom", "ali_v")
        self.assertIn("Telefon: +998901234567", result)

    def test_start_broadcast_job_schedules_message_every_two_minutes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.save_setting("group_chat_id", "-1001234567890")
                context = Mock()
                context.job_queue.get_jobs_by_name.return_value = []

                started = telegram_bot.start_broadcast_job(context, "Salom guruh")

                self.assertTrue(started)
                self.assertEqual(telegram_bot.load_setting("broadcast_message"), "Salom guruh")
                self.assertEqual(telegram_bot.load_setting("broadcast_active"), "1")
                context.job_queue.run_repeating.assert_called_once_with(
                    telegram_bot.send_periodic_broadcast,
                    interval=120,
                    first=1,
                    name="broadcast_job",
                )
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_interval_menu_offers_two_through_eight_minutes(self):
        markup = telegram_bot.build_broadcast_interval_menu(5)
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertEqual(
            callbacks[:-1],
            [f"set_broadcast_interval_{minutes}" for minutes in range(2, 9)],
        )
        self.assertIn("✅ 5 daqiqa", [button.text for row in markup.inline_keyboard for button in row])

    def test_user_broadcast_job_uses_selected_interval(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.save_setting("group_chat_id", "-1001234567890")
                context = Mock()
                context.job_queue.get_jobs_by_name.return_value = []

                started = telegram_bot.start_user_broadcast_job(context, 123456, "Salom", 7)

                self.assertTrue(started)
                context.job_queue.run_repeating.assert_called_once_with(
                    telegram_bot.send_periodic_broadcast,
                    interval=420,
                    first=1,
                    name="user_broadcast_123456",
                    data={"user_id": 123456},
                )
                conn = sqlite3.connect(telegram_bot.DB_PATH)
                row = conn.execute(
                    "SELECT interval_minutes, is_active FROM user_broadcasts WHERE user_id = ?",
                    (123456,),
                ).fetchone()
                conn.close()
                self.assertEqual(row, (7, 1))
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_group_message_stats_count_last_day_and_week(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                now = datetime(2026, 10, 16, 12, 0)
                telegram_bot.record_group_message_sent(111111, now - timedelta(days=8))
                telegram_bot.record_group_message_sent(111111, now - timedelta(days=3))
                telegram_bot.record_group_message_sent(111111, now - timedelta(hours=12))
                telegram_bot.record_group_message_sent(222222, now - timedelta(hours=1))

                self.assertEqual(telegram_bot.get_group_message_stats(now), (2, 3))
                self.assertEqual(
                    telegram_bot.get_group_message_stats(now, sender_user_id=111111),
                    (1, 2),
                )
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_init_db_adds_sender_column_to_existing_group_message_log(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                conn = sqlite3.connect(telegram_bot.DB_PATH)
                conn.execute(
                    "CREATE TABLE group_message_sends (id INTEGER PRIMARY KEY, sent_at TEXT NOT NULL)"
                )
                conn.commit()
                conn.close()

                telegram_bot.init_db()

                conn = sqlite3.connect(telegram_bot.DB_PATH)
                columns = {row[1] for row in conn.execute("PRAGMA table_info(group_message_sends)")}
                conn.close()
                self.assertIn("sender_user_id", columns)
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_user_stats_callback_shows_only_requesting_users_sends(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                now = datetime.now()
                telegram_bot.record_group_message_sent(123456, now - timedelta(hours=1))
                telegram_bot.record_group_message_sent(123456, now - timedelta(days=3))
                telegram_bot.record_group_message_sent(654321, now - timedelta(hours=2))
                update = Mock()
                update.effective_user.id = 123456
                update.callback_query.data = "user_stats"
                update.callback_query.answer = AsyncMock()
                update.callback_query.edit_message_text = AsyncMock()
                context = Mock()
                context.user_data = {}

                asyncio.run(telegram_bot.handle_callback(update, context))

                stats_text = update.callback_query.edit_message_text.await_args.args[0]
                self.assertIn("Siz yuborgan xabarlar (oxirgi 1 kun): 1 ta", stats_text)
                self.assertIn("Siz yuborgan xabarlar (oxirgi 1 hafta): 2 ta", stats_text)
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_successful_periodic_group_send_is_counted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.save_setting("group_chat_id", "-1001234567890")
                telegram_bot.save_setting("broadcast_message", "Salom guruh")
                telegram_bot.save_setting("broadcast_user_id", str(telegram_bot.OWNER_ID))
                context = Mock()
                context.bot.send_message = AsyncMock()

                asyncio.run(telegram_bot.send_periodic_broadcast(context))

                context.bot.send_message.assert_awaited_once()
                self.assertEqual(telegram_bot.get_group_message_stats(), (1, 1))
                self.assertEqual(
                    telegram_bot.get_group_message_stats(sender_user_id=telegram_bot.OWNER_ID),
                    (1, 1),
                )
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_user_broadcast_interval_is_saved_per_user(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.set_user_broadcast_interval(123456, 8)
                self.assertEqual(telegram_bot.get_user_broadcast_interval(123456), 8)
                self.assertEqual(telegram_bot.get_user_broadcast_interval(654321), 2)
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_interval_callback_saves_choice_for_premium_user(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.grant_premium_for_user(123456, 1)
                update = Mock()
                update.effective_user.id = 123456
                update.callback_query.data = "set_broadcast_interval_6"
                update.callback_query.answer = AsyncMock()
                update.callback_query.edit_message_text = AsyncMock()
                context = Mock()
                context.user_data = {}

                asyncio.run(telegram_bot.handle_callback(update, context))

                self.assertEqual(telegram_bot.get_user_broadcast_interval(123456), 6)
                update.callback_query.edit_message_text.assert_awaited_once()
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_resume_broadcast_requires_active_premium(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.save_setting("group_chat_id", "-1001234567890")
                telegram_bot.save_setting("broadcast_message", "Salom guruh")
                telegram_bot.save_setting("broadcast_user_id", "123456")
                telegram_bot.save_setting("broadcast_active", "1")
                context = Mock()
                context.job_queue.get_jobs_by_name.return_value = []

                resumed = telegram_bot.resume_broadcast_job(context)

                self.assertFalse(resumed)
                self.assertEqual(telegram_bot.load_setting("broadcast_active"), "0")
                context.job_queue.run_repeating.assert_not_called()
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_pending_broadcast_message_is_rejected_without_premium(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.save_user_profile(123456, name="Ali", phone="+998901234567")
                update = Mock()
                update.effective_user.id = 123456
                update.message.text = "Salom guruh"
                update.message.reply_text = AsyncMock()
                context = Mock()
                context.user_data = {"pending_broadcast_message": True}

                asyncio.run(telegram_bot.text_echo(update, context))

                self.assertFalse(context.user_data["pending_broadcast_message"])
                update.message.reply_text.assert_awaited_once_with(
                    "Avtomatik xabar yuborish uchun faol Pro obuna kerak."
                )
                self.assertEqual(telegram_bot.load_setting("broadcast_message"), "")
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_user_menus_do_not_contain_admin_button(self):
        main_menu = telegram_bot.build_main_menu()
        unpaid_menu = telegram_bot.build_unpaid_menu()
        for menu in (main_menu, unpaid_menu):
            labels = [button.text for row in menu.inline_keyboard for button in row]
            self.assertFalse(any("Admin" in label for label in labels))
        main_labels = [button.text for row in main_menu.inline_keyboard for button in row]
        unpaid_labels = [button.text for row in unpaid_menu.inline_keyboard for button in row]
        self.assertIn("💎 Pro", main_labels)
        self.assertIn("👤 Profil", main_labels)
        self.assertIn("💎 Pro sotib olish", unpaid_labels)
        self.assertIn("💰 Balans", main_labels)
        self.assertIn("💰 Balans", unpaid_labels)
        self.assertIn("📊 Statistika", main_labels)
        self.assertFalse(any("Xabar yozish" in label for label in main_labels))

    def test_admin_button_is_visible_only_in_admin_main_menu(self):
        admin_labels = [
            button.text
            for row in telegram_bot.build_main_menu(is_admin=True).inline_keyboard
            for button in row
        ]
        user_labels = [
            button.text
            for row in telegram_bot.build_main_menu(is_admin=False).inline_keyboard
            for button in row
        ]
        self.assertIn("⚙️ Admin", admin_labels)
        self.assertNotIn("⚙️ Admin", user_labels)
        self.assertIn("user_stats", [
            button.callback_data
            for row in telegram_bot.build_main_menu(is_admin=False).inline_keyboard
            for button in row
        ])
        admin_panel_callbacks = [
            button.callback_data
            for row in telegram_bot.build_admin_menu().inline_keyboard
            for button in row
        ]
        self.assertIn("admin_premium_users", admin_panel_callbacks)
        self.assertIn("stats", admin_panel_callbacks)
        admin_panel_labels = [
            button.text
            for row in telegram_bot.build_admin_menu().inline_keyboard
            for button in row
        ]
        self.assertIn("💰 Pro narxlari", admin_panel_labels)
        self.assertIn("👥 Pro foydalanuvchilar", admin_panel_labels)
        self.assertIn("admin_channel", admin_panel_callbacks)
        self.assertIn("admin_add_balance", admin_panel_callbacks)
        self.assertIn("admin_remove_balance", admin_panel_callbacks)

    def test_user_balance_defaults_to_zero_and_additions_accumulate(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                self.assertEqual(telegram_bot.get_user_balance(123456), 0)
                self.assertEqual(telegram_bot.add_user_balance(123456, 50000), 50000)
                self.assertEqual(telegram_bot.add_user_balance(123456, 25000), 75000)
                self.assertEqual(telegram_bot.get_user_balance(123456), 75000)
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_admin_can_add_balance_by_username(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.save_user_profile(123456, username="ali_v")
                update = Mock()
                update.effective_user.id = telegram_bot.OWNER_ID
                update.callback_query.data = "admin_add_balance"
                update.callback_query.answer = AsyncMock()
                update.callback_query.edit_message_text = AsyncMock()
                update.message.text = "@ali_v 3000"
                update.message.reply_text = AsyncMock()
                context = Mock()
                context.bot.send_message = AsyncMock()
                context.user_data = {}

                asyncio.run(telegram_bot.handle_callback(update, context))
                asyncio.run(telegram_bot.text_echo(update, context))

                self.assertEqual(telegram_bot.get_user_balance(123456), 3000)
                self.assertIn("3.000 so'm qo'shildi", update.message.reply_text.await_args.args[0])
                context.bot.send_message.assert_awaited_once()
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_admin_can_remove_balance_without_allowing_negative_amounts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.add_user_balance(123456, 50000)
                telegram_bot.save_user_profile(123456, username="ali_v")
                update = Mock()
                update.effective_user.id = telegram_bot.OWNER_ID
                update.callback_query.data = "admin_remove_balance"
                update.callback_query.answer = AsyncMock()
                update.callback_query.edit_message_text = AsyncMock()
                update.message.text = "@ali_v 20000"
                update.message.reply_text = AsyncMock()
                context = Mock()
                context.bot.send_message = AsyncMock()
                context.user_data = {}

                asyncio.run(telegram_bot.handle_callback(update, context))
                self.assertTrue(context.user_data["pending_balance_remove"])

                asyncio.run(telegram_bot.text_echo(update, context))
                self.assertEqual(telegram_bot.get_user_balance(123456), 30000)
                self.assertIn("Yangi balans: 30.000 so'm", update.message.reply_text.await_args.args[0])
                context.bot.send_message.assert_awaited_once()

                context.user_data["pending_balance_remove"] = True
                update.message.text = "123456 40000"
                update.message.reply_text.reset_mock()
                asyncio.run(telegram_bot.text_echo(update, context))
                self.assertEqual(telegram_bot.get_user_balance(123456), 30000)
                self.assertIn("Balans yetarli emas", update.message.reply_text.await_args.args[0])
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_pro_purchase_from_balance_deducts_price_and_grants_access(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.add_user_balance(123456, 150000)

                purchased, expires_at, balance, price = telegram_bot.purchase_pro_with_balance(123456, 1)

                self.assertTrue(purchased)
                self.assertIsInstance(expires_at, datetime)
                self.assertEqual(price, 100000)
                self.assertEqual(balance, 50000)
                self.assertEqual(telegram_bot.get_user_balance(123456), 50000)
                self.assertTrue(telegram_bot.is_allowed(123456))
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_pro_purchase_with_insufficient_balance_changes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.add_user_balance(123456, 99999)

                purchased, expires_at, balance, price = telegram_bot.purchase_pro_with_balance(123456, 1)

                self.assertFalse(purchased)
                self.assertIsNone(expires_at)
                self.assertEqual(balance, 99999)
                self.assertEqual(price, 100000)
                self.assertEqual(telegram_bot.get_user_balance(123456), 99999)
                self.assertFalse(telegram_bot.is_allowed(123456))
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_pro_purchase_menu_has_balance_and_admin_options(self):
        markup = telegram_bot.build_pro_purchase_menu(3, 25000)
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        self.assertEqual(callbacks, ["buy_balance_3m", "buy_admin_3m", "premium"])

    def test_pro_menu_includes_one_day_and_one_week(self):
        callbacks = [
            button.callback_data
            for row in telegram_bot.build_premium_menu().inline_keyboard
            for button in row
        ]
        self.assertIn("plan_1d", callbacks)
        self.assertIn("plan_7d", callbacks)

    def test_daily_and_weekly_prices_are_configurable(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                self.assertEqual(telegram_bot.get_pro_price("1d"), 3333)
                self.assertEqual(telegram_bot.get_pro_price("7d"), 23333)
                telegram_bot.set_pro_price("1d", 5000)
                telegram_bot.set_pro_price("7d", 30000)
                self.assertEqual(telegram_bot.get_pro_price("1d"), 5000)
                self.assertEqual(telegram_bot.get_pro_price("7d"), 30000)
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_balance_purchase_supports_one_day_plan(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.add_user_balance(123456, 5000)
                started_at = datetime.now()

                purchased, expires_at, balance, price = telegram_bot.purchase_pro_with_balance(123456, "1d")

                self.assertTrue(purchased)
                self.assertAlmostEqual((expires_at - started_at).total_seconds(), 86400, delta=2)
                self.assertEqual(price, 3333)
                self.assertEqual(balance, 1667)
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_admin_approval_grants_one_week_from_request(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.save_premium_request(123456, "7d", name="Ali", phone="+998901234567")
                started_at = datetime.now()

                approved, expires_at = telegram_bot.approve_premium_request(123456)

                self.assertTrue(approved)
                self.assertAlmostEqual((expires_at - started_at).total_seconds(), 7 * 86400, delta=2)
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_referral_is_saved_and_bonus_is_awarded_only_once(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                self.assertTrue(telegram_bot.create_referral(111111, 222222))
                self.assertFalse(telegram_bot.create_referral(222222, 222222))
                self.assertFalse(telegram_bot.create_referral(333333, 222222))

                reward = telegram_bot.reward_referrer_on_pro_activation(222222)

                self.assertEqual(reward, (111111, 10000, 10000))
                self.assertIsNone(telegram_bot.reward_referrer_on_pro_activation(222222))
                self.assertEqual(telegram_bot.get_user_balance(111111), 10000)
                self.assertEqual(telegram_bot.get_referral_stats(111111), (1, 1))
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_referral_link_contains_bot_and_inviter(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                update = Mock()
                update.effective_user.id = telegram_bot.OWNER_ID
                update.callback_query.data = "referral"
                update.callback_query.answer = AsyncMock()
                update.callback_query.edit_message_text = AsyncMock()
                context = Mock()
                context.bot.get_me = AsyncMock(return_value=Mock(username="sample_bot"))

                asyncio.run(telegram_bot.handle_callback(update, context))

                message = update.callback_query.edit_message_text.await_args.args[0]
                self.assertIn("https://t.me/sample_bot?start=ref_", message)
                self.assertIn("10.000 so'm", message)
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_start_referral_is_recorded_before_phone_registration(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                update = Mock()
                update.effective_user.id = 222222
                update.effective_user.username = None
                update.message.reply_text = AsyncMock()
                context = Mock()
                context.args = ["ref_111111"]

                asyncio.run(telegram_bot.start(update, context))

                self.assertEqual(telegram_bot.get_referral_stats(111111), (1, 0))
                self.assertEqual(telegram_bot.load_user_profile(222222), ("", ""))
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_start_saves_username_before_phone_registration(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                update = Mock()
                update.effective_user.id = 222222
                update.effective_user.full_name = "Ali"
                update.effective_user.username = "ali_v"
                update.message.reply_text = AsyncMock()
                context = Mock()
                context.args = []

                asyncio.run(telegram_bot.start(update, context))

                self.assertEqual(telegram_bot.load_user_username(222222), "ali_v")
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_balance_checkout_callback_activates_pro(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.save_user_profile(123456, name="Ali", phone="+998901234567")
                telegram_bot.add_user_balance(123456, 150000)
                update = Mock()
                update.effective_user.id = 123456
                update.effective_user.full_name = "Ali"
                update.effective_user.username = "ali_v"
                update.callback_query.data = "plan_1m"
                update.callback_query.answer = AsyncMock()
                update.callback_query.edit_message_text = AsyncMock()
                context = Mock()
                context.user_data = {}

                asyncio.run(telegram_bot.handle_callback(update, context))
                checkout = update.callback_query.edit_message_text.await_args
                callbacks = [
                    button.callback_data
                    for row in checkout.kwargs["reply_markup"].inline_keyboard
                    for button in row
                ]
                self.assertEqual(callbacks[:2], ["buy_balance_1m", "buy_admin_1m"])

                update.callback_query.data = "buy_balance_1m"
                update.callback_query.edit_message_text.reset_mock()
                asyncio.run(telegram_bot.handle_callback(update, context))

                self.assertTrue(telegram_bot.is_allowed(123456))
                self.assertEqual(telegram_bot.get_user_balance(123456), 50000)
                self.assertIn("balans", update.callback_query.edit_message_text.await_args.args[0].lower())
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_admin_checkout_callback_creates_pending_request(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.save_user_profile(123456, name="Ali", phone="+998901234567")
                update = Mock()
                update.effective_user.id = 123456
                update.effective_user.username = "ali_v"
                update.callback_query.data = "buy_admin_3m"
                update.callback_query.answer = AsyncMock()
                update.callback_query.edit_message_text = AsyncMock()
                context = Mock()
                context.bot.send_message = AsyncMock()
                context.user_data = {}

                asyncio.run(telegram_bot.handle_callback(update, context))

                self.assertEqual(
                    telegram_bot.list_pending_premium_requests()[0][0:5],
                    (123456, 3, 90, "Ali", "+998901234567"),
                )
                context.bot.send_message.assert_awaited_once()
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_balance_adjustment_parser(self):
        self.assertEqual(telegram_bot.parse_balance_adjustment("123456 50000"), (123456, 50000))
        self.assertEqual(telegram_bot.parse_balance_adjustment("123456 50.000"), (123456, 50000))
        self.assertIsNone(telegram_bot.parse_balance_adjustment("123456 0"))
        self.assertIsNone(telegram_bot.parse_balance_adjustment("not-id 50000"))
        self.assertEqual(telegram_bot.parse_balance_target("123456 50.000"), ("123456", 50000))
        self.assertEqual(telegram_bot.parse_balance_target("@ali_v 50000"), ("@ali_v", 50000))
        self.assertEqual(telegram_bot.parse_balance_target("ali_v 50000"), ("ali_v", 50000))
        self.assertIsNone(telegram_bot.parse_balance_target("@ 50000"))

    def test_required_channel_parser_supports_public_and_private_channels(self):
        self.assertEqual(
            telegram_bot.parse_required_channel("@my_channel"),
            ("@my_channel", "https://t.me/my_channel"),
        )
        self.assertEqual(
            telegram_bot.parse_required_channel("-1001234567890 https://t.me/+invite"),
            ("-1001234567890", "https://t.me/+invite"),
        )
        self.assertIsNone(telegram_bot.parse_required_channel("invalid"))
        self.assertIsNone(telegram_bot.parse_required_channel("-1001234567890"))

    def test_admin_can_configure_required_group(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                update = Mock()
                update.effective_user.id = telegram_bot.OWNER_ID
                update.callback_query.data = "admin_group_set"
                update.callback_query.answer = AsyncMock()
                update.callback_query.edit_message_text = AsyncMock()
                context = Mock()
                context.user_data = {}

                asyncio.run(telegram_bot.handle_callback(update, context))
                self.assertTrue(context.user_data["pending_required_group"])

                update.message.text = "-1001234567890 https://t.me/+invite"
                update.message.reply_text = AsyncMock()
                asyncio.run(telegram_bot.text_echo(update, context))

                self.assertEqual(telegram_bot.load_setting("required_group"), "-1001234567890")
                self.assertEqual(
                    telegram_bot.load_setting("required_group_link"),
                    "https://t.me/+invite",
                )
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_group_gate_requires_both_channel_and_group_membership(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.save_setting("required_channel", "@my_channel")
                telegram_bot.save_setting("required_channel_link", "https://t.me/my_channel")
                telegram_bot.save_setting("required_group", "-1001234567890")
                telegram_bot.save_setting("required_group_link", "https://t.me/+invite")
                update = Mock()
                update.effective_user.id = 123456
                update.callback_query = None
                update.effective_message.reply_text = AsyncMock()
                context = Mock()
                context.bot.get_chat_member = AsyncMock(
                    side_effect=[Mock(status="member"), Mock(status="left")]
                )

                with self.assertRaises(telegram_bot.ApplicationHandlerStop):
                    asyncio.run(telegram_bot.required_channel_gate(update, context))

                context.bot.get_chat_member.assert_has_awaits(
                    [
                        unittest.mock.call(chat_id="@my_channel", user_id=123456),
                        unittest.mock.call(chat_id="-1001234567890", user_id=123456),
                    ]
                )
                buttons = [
                    button
                    for row in update.effective_message.reply_text.await_args.kwargs[
                        "reply_markup"
                    ].inline_keyboard
                    for button in row
                ]
                self.assertEqual(buttons[0].url, "https://t.me/my_channel")
                self.assertEqual(buttons[1].url, "https://t.me/+invite")
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_channel_gate_blocks_non_subscriber(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.save_setting("required_channel", "@my_channel")
                telegram_bot.save_setting("required_channel_link", "https://t.me/my_channel")
                update = Mock()
                update.effective_user.id = 123456
                update.callback_query = None
                update.effective_message.reply_text = AsyncMock()
                context = Mock()
                context.bot.get_chat_member = AsyncMock(return_value=Mock(status="left"))

                with self.assertRaises(telegram_bot.ApplicationHandlerStop):
                    asyncio.run(telegram_bot.required_channel_gate(update, context))

                reply = update.effective_message.reply_text.await_args
                buttons = [button for row in reply.kwargs["reply_markup"].inline_keyboard for button in row]
                self.assertEqual(buttons[0].url, "https://t.me/my_channel")
                self.assertEqual(buttons[1].callback_data, "check_channel_subscription")
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_channel_membership_accepts_subscriber_statuses(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.save_setting("required_channel", "@my_channel")
                for status in ("member", "administrator", "creator"):
                    context = Mock()
                    context.bot.get_chat_member = AsyncMock(return_value=Mock(status=status))
                    self.assertTrue(asyncio.run(telegram_bot.is_user_subscribed(context, 123456)))
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_verified_channel_user_is_prompted_to_buy_premium(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.save_user_profile(123456, name="Ali", phone="+998901234567")
                update = Mock()
                update.effective_user.id = 123456
                update.effective_user.full_name = "Ali"
                update.effective_user.username = "ali_v"
                update.callback_query.edit_message_text = AsyncMock()
                context = Mock()

                asyncio.run(telegram_bot.continue_after_channel_check(update, context))

                reply = update.callback_query.edit_message_text.await_args
                self.assertIn("Pro paketni tanlang", reply.args[0])
                callbacks = [
                    button.callback_data
                    for row in reply.kwargs["reply_markup"].inline_keyboard
                    for button in row
                ]
                self.assertIn("plan_1m", callbacks)
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_active_premium_user_list_includes_profile_details(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.save_user_profile(
                    123456,
                    name="Ali Valiyev",
                    phone="+998901234567",
                    username="ali_v",
                )
                telegram_bot.grant_premium_for_user(123456, 1)

                users = telegram_bot.list_active_premium_users()

                self.assertEqual(len(users), 1)
                self.assertEqual(users[0][:4], (123456, "ali_v", "Ali Valiyev", "+998901234567"))
                self.assertTrue(users[0][4])
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_premium_user_list_sends_each_profile_with_its_own_button(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                for user_id, username in ((123456, "ali_v"), (654321, "vali_a")):
                    telegram_bot.save_user_profile(
                        user_id,
                        name=f"User {user_id}",
                        phone=f"+998{user_id}",
                        username=username,
                    )
                    telegram_bot.grant_premium_for_user(user_id, 1)

                update = Mock()
                update.effective_user.id = telegram_bot.OWNER_ID
                update.effective_chat.id = telegram_bot.OWNER_ID
                update.callback_query.data = "admin_premium_users"
                update.callback_query.answer = AsyncMock()
                update.callback_query.edit_message_text = AsyncMock()
                context = Mock()
                context.user_data = {}
                context.bot.send_message = AsyncMock()

                with patch("telegram_bot.asyncio.sleep", new_callable=AsyncMock):
                    asyncio.run(telegram_bot.handle_callback(update, context))

                sent_messages = context.bot.send_message.await_args_list
                self.assertEqual(len(sent_messages), 2)
                for sent_message in sent_messages:
                    text = sent_message.kwargs["text"]
                    user_id = int(text.split("ID: ", 1)[1].splitlines()[0])
                    username = "ali_v" if user_id == 123456 else "vali_a"
                    self.assertIn(f"@{username}", text)
                    self.assertEqual(
                        sent_message.kwargs["reply_markup"].inline_keyboard[0][0].callback_data,
                        f"premium_revoke_{user_id}",
                    )
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_revoke_premium_access_removes_user_access(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.grant_premium_for_user(123456, 1)
                self.assertTrue(telegram_bot.is_allowed(123456))

                self.assertTrue(telegram_bot.revoke_premium_access(123456))
                self.assertFalse(telegram_bot.is_allowed(123456))
                self.assertFalse(telegram_bot.revoke_premium_access(123456))
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_profile_pro_remaining_shows_time_left(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                now = datetime(2026, 10, 15, 12, 0)
                expiry = now + timedelta(days=2, hours=3, minutes=4)
                conn = sqlite3.connect(telegram_bot.DB_PATH)
                conn.execute(
                    "INSERT INTO premium_users (user_id, premium_until) VALUES (?, ?)",
                    (123456, expiry.isoformat()),
                )
                conn.commit()
                conn.close()

                self.assertEqual(
                    telegram_bot.format_pro_remaining(123456, now),
                    "2 kun 3 soat (tugash: 17.10.2026 15:04)",
                )
                self.assertEqual(telegram_bot.format_pro_remaining(654321, now), "Faol Pro yo'q")
                self.assertEqual(
                    telegram_bot.format_pro_remaining(telegram_bot.OWNER_ID, now),
                    "Muddatsiz (admin)",
                )
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_premium_user_action_button_targets_user(self):
        markup = telegram_bot.build_premium_user_actions([123456])
        button = markup.inline_keyboard[0][0]
        self.assertEqual(button.text, "🚫 Ruxsatni olib qo'yish")
        self.assertEqual(button.callback_data, "premium_revoke_123456")

    def test_main_entry_point_exists(self):
        self.assertTrue(callable(getattr(telegram_bot, "main", None)))

    def test_load_bot_token_reads_telegram_env_var(self):
        old_value = os.environ.get("TELEGRAM_BOT_TOKEN")
        try:
            os.environ["TELEGRAM_BOT_TOKEN"] = "123456:ABCDEF"
            os.environ.pop("BOT_TOKEN", None)
            self.assertEqual(telegram_bot.load_bot_token(), "123456:ABCDEF")
        finally:
            if old_value is None:
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            else:
                os.environ["TELEGRAM_BOT_TOKEN"] = old_value

    def test_grant_premium_for_user_sets_expiry(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "allowed_users.db"
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = db_path
            try:
                telegram_bot.init_db()
                expires_at = telegram_bot.grant_premium_for_user(123456, 1)
                self.assertIsInstance(expires_at, datetime)
                self.assertTrue(telegram_bot.is_premium_active(123456))
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_allow_list_does_not_grant_access_without_premium(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.add_allowed_user(123456)
                self.assertFalse(telegram_bot.is_allowed(123456))
                telegram_bot.grant_premium_for_user(123456, 1)
                self.assertTrue(telegram_bot.is_allowed(123456))
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_premium_request_keyboard_has_accept_and_reject(self):
        keyboard = telegram_bot.build_premium_request_keyboard(123456).inline_keyboard
        self.assertEqual(keyboard[0][0].callback_data, "premium_approve_123456")
        self.assertEqual(keyboard[1][0].callback_data, "premium_reject_123456")

    def test_user_profile_saves_name_and_phone(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.save_user_profile(123456, name="Ali Valiyev", phone="+998901234567")
                self.assertEqual(
                    telegram_bot.load_user_profile(123456),
                    ("Ali Valiyev", "+998901234567"),
                )
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_first_start_requests_phone_contact(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                telegram_bot.save_user_profile(123456, name="Ali", phone="+998")
                update = Mock()
                update.effective_user.id = 123456
                update.effective_user.username = None
                update.message.reply_text = AsyncMock()

                asyncio.run(telegram_bot.start(update, Mock()))

                reply = update.message.reply_text.await_args
                self.assertIn("telefon", reply.args[0].lower())
                self.assertTrue(reply.kwargs["reply_markup"].keyboard[0][0].request_contact)
                self.assertEqual(telegram_bot.load_user_profile(123456), ("Ali", "+998"))
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_admin_without_phone_is_blocked_until_contact_registration(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                update = Mock()
                update.effective_user.id = telegram_bot.OWNER_ID
                update.effective_user.username = "admin"
                update.callback_query = None
                update.effective_message.contact = None
                update.effective_message.reply_text = AsyncMock()

                with self.assertRaises(telegram_bot.ApplicationHandlerStop):
                    asyncio.run(telegram_bot.registration_gate(update, Mock()))

                reply = update.effective_message.reply_text.await_args
                self.assertTrue(reply.kwargs["reply_markup"].keyboard[0][0].request_contact)
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_phone_registration_requires_real_number(self):
        self.assertFalse(telegram_bot.has_registered_phone("+998"))
        self.assertFalse(telegram_bot.has_registered_phone(""))
        self.assertTrue(telegram_bot.has_registered_phone("+998901234567"))

    def test_shared_contact_is_saved_to_user_profile(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                update = Mock()
                update.effective_user.id = 123456
                update.effective_user.full_name = "Ali Valiyev"
                update.effective_user.username = "ali_v"
                update.message.contact.user_id = 123456
                update.message.contact.phone_number = "+998901234567"
                update.message.reply_text = AsyncMock()

                asyncio.run(telegram_bot.handle_contact(update, Mock()))

                self.assertEqual(
                    telegram_bot.load_user_profile(123456),
                    ("Ali Valiyev", "+998901234567"),
                )
                conn = sqlite3.connect(telegram_bot.DB_PATH)
                username = conn.execute(
                    "SELECT username FROM user_profiles WHERE user_id = ?", (123456,)
                ).fetchone()[0]
                conn.close()
                self.assertEqual(username, "ali_v")
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_premium_prices_are_saved_and_formatted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            old_db_path = telegram_bot.DB_PATH
            telegram_bot.DB_PATH = Path(tmp_dir) / "allowed_users.db"
            try:
                telegram_bot.init_db()
                self.assertEqual(telegram_bot.get_premium_price(1), 100000)
                telegram_bot.set_premium_price(1, 175000)
                self.assertEqual(telegram_bot.get_premium_price(1), 175000)
                self.assertIn("1 oy - 175.000", telegram_bot.premium_prices_text())
            finally:
                telegram_bot.DB_PATH = old_db_path

    def test_parse_premium_price_accepts_formatted_positive_values(self):
        self.assertEqual(telegram_bot.parse_premium_price("175000"), 175000)
        self.assertEqual(telegram_bot.parse_premium_price("175.000"), 175000)
        self.assertEqual(telegram_bot.parse_premium_price(" 175 000 "), 175000)
        self.assertIsNone(telegram_bot.parse_premium_price("0"))
        self.assertIsNone(telegram_bot.parse_premium_price("narx"))


if __name__ == "__main__":
    unittest.main()
