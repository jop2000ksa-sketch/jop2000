import os
import re
import logging
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from telegram.constants import ParseMode
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# =========================
# إعداد السجلات
# =========================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# =========================
# المتغيرات العامة
# =========================
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("Set TOKEN env var")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "super-secret-path")

# نفضّل PUBLIC_URL لو موجود، وإلا نرجع لـ RENDER_EXTERNAL_URL
APP_URL = os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL")


# جلسات العمل
admin_sessions: dict[int, dict] = {}   # جلسات النشر لكل مشرف (target_channel_id يُخزَّن هنا بعد الربط)
admin_inquiries: dict[int, dict] = {}  # جلسات الاستفسار لكل مستخدم

# =========================
# أدوات مساعدة
# =========================
# === أوامر تشخيص مفيدة داخل تيليجرام ===
async def webhookinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info = await context.bot.get_webhook_info()
    await update.message.reply_text(
        f"url: {info.url}\n"
        f"pending: {info.pending_update_count}\n"
        f"last_error: {info.last_error_date} {info.last_error_message if info.last_error_date else ''}"
    )

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sess = admin_sessions.get(uid)
    await update.message.reply_text(
        f"session_open: {bool(sess and sess.get('awaiting_input'))}\n"
        f"target_channel_id: {sess.get('target_channel_id') if sess else None}"
    )

async def reset_publish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    sess = admin_sessions.get(uid)
    if not sess:
        await update.message.reply_text("لا توجد جلسة نشر حالية.")
        return
    target = sess.get("target_channel_id")
    admin_sessions[uid] = {"target_channel_id": target} if target else {}
    await update.message.reply_text("✅ تم إنهاء جلسة النشر. اكتب jop لبدء جلسة جديدة.")

def auto_hide_links(text: str) -> str:
    return re.sub(r'(https?://\S+)', r'<a href="\1">اضغط هنا</a>', text or "")

async def get_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    me = await context.bot.get_me()
    return me.username

async def is_admin_in_chat(context: ContextTypes.DEFAULT_TYPE, chat_id: int | None, user_id: int) -> bool:
    if not chat_id:
        return False
    try:
        m = await context.bot.get_chat_member(chat_id, user_id)
        return m.status in ("administrator", "creator")
    except Exception:
        return False

# =========================
# ربط قناة/مجموعة الهدف للنشر (بدون /bind_here)
# عبر إعادة توجيه منشور من القناة/المجموعة للخاص مع البوت
# =========================
async def handle_jop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    sess = admin_sessions.get(user.id, {})
    target = sess.get("target_channel_id")

    # لازم يكون فيه ربط مسبق عبر إعادة توجيه منشور من القناة/المجموعة
    if not target:
        await update.message.reply_text(
            "⚠️ قبل البدء: أعد توجيه منشور من القناة/المجموعة إلى الخاص هنا لربط وجهة النشر."
        )
        return

    # لو فيه جلسة نشر مفتوحة، لا نعيد رسالة الترحيب
    if sess.get("awaiting_input"):
        await update.message.reply_text("ℹ️ لديك جلسة نشر مفتوحة. أرسل المحتوى الآن أو اضغط ✅ تم.")
        return

    # افتح جلسة جديدة
    admin_sessions[user.id] = {
        "text": None,
        "media": None,
        "awaiting_input": True,
        "target_channel_id": target,
        "controls_msg_id": None,
        "controls_chat_id": None,
    }

    await update.message.reply_text(
        text=(
            f"🧑‍💼 *المشرف:* `{user.full_name}`\n\n"
            "📝 *مرحبًا بك في نظام النشر.*\n"
            "✏️ أرسل *نص المنشور* أو وسائط:\n"
            "- صورة\n- فيديو\n- ملف\n- صوت\n\n"
            "✅ بعد الانتهاء اضغط زر *(تم)* لإرسال المنشور."
        ),
        parse_mode=ParseMode.MARKDOWN
    )

async def bind_by_username(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        return
    if not context.args:
        await update.message.reply_text("اكتب هكذا:\n/bind @اسم_القناة", parse_mode=ParseMode.MARKDOWN)
        return

    username = context.args[0].strip()
    if not username.startswith("@"):
        await update.message.reply_text("اكتب اسم القناة مع @ مثل: /bind @mychannel")
        return

    try:
        chat = await context.bot.get_chat(username)
    except Exception:
        await update.message.reply_text("⚠️ لم أستطع الوصول لهذه القناة. تأكد من الاسم وأن البوت مضاف هناك.")
        return

    try:
        member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
        if member.status not in ("administrator", "creator"):
            await update.message.reply_text("⚠️ يجب أن تكون *مشرفًا* في القناة.", parse_mode=ParseMode.MARKDOWN)
            return
    except Exception:
        await update.message.reply_text("⚠️ تعذّر التحقق من صلاحيتك. هل البوت أدمن في القناة؟")
        return

    sess = admin_sessions.setdefault(update.effective_user.id, {})
    sess["target_channel_id"] = chat.id
    await update.message.reply_text(f"✅ تم الربط بهذه الوجهة للنشر.\nID: {chat.id}")

async def bind_from_forward(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    user = update.effective_user

    # الخاص فقط
    if not msg or not user or update.effective_chat.type != "private":
        return

    # التعرّف على إعادة التوجيه (القديم + الجديد)
    fchat = None
    fchat_type = None

    if getattr(msg, "forward_from_chat", None):
        fchat = msg.forward_from_chat
        fchat_type = fchat.type
    elif getattr(msg, "forward_origin", None):
        try:
            fchat = msg.forward_origin.chat
            fchat_type = getattr(fchat, "type", None)
        except Exception:
            fchat = None

    # لو ليست إعادة توجيه: لا ترسل تحذير إلا لو المستخدم يطلب ربطاً صراحة
    if not fchat:
        text = (msg.text or msg.caption or "").strip().lower()
        if any(k in text for k in (" /bind", "/bind", "bind", "ربط")):
            await msg.reply_text(
                "⚠️ هذه ليست *إعادة توجيه* من القناة.\n"
                "لربط القناة بتحويل منشور: افتح القناة → اختر رسالة → **Forward** (بدون Hide sender) → أرسلها هنا.\n"
                "أو استخدم: `/bind @username`",
                parse_mode=ParseMode.MARKDOWN
            )
        return

    if fchat_type not in ("channel", "supergroup", "group"):
        await msg.reply_text("⚠️ أعد توجيه منشور من *قناة* أو *مجموعة* فقط.", parse_mode=ParseMode.MARKDOWN)
        return

    # تأكد وصول البوت للقناة
    try:
        await context.bot.get_chat(fchat.id)
    except Exception:
        await msg.reply_text(
            "⚠️ لا أستطيع الوصول إلى القناة.\n"
            "أضِف البوت كـ *مشرف* في القناة أولًا ثم أعد التوجيه.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # تأكد أنك مشرف
    try:
        member = await context.bot.get_chat_member(fchat.id, user.id)
        if member.status not in ("administrator", "creator"):
            await msg.reply_text("⚠️ يجب أن تكون *مشرفًا* في القناة المعاد توجيهها.", parse_mode=ParseMode.MARKDOWN)
            return
    except Exception:
        await msg.reply_text("⚠️ تعذّر التحقق من صلاحيتك هناك. تأكد من أن البوت أدمن ثم أعد المحاولة.")
        return

    # احفظ الربط
    sess = admin_sessions.setdefault(user.id, {})
    sess["target_channel_id"] = fchat.id
    await msg.reply_text(f"✅ تم الربط بهذه الوجهة للنشر.\nID: {fchat.id}")

# =========================
# تحضير النشر للأدمن (ديناميكي)
# =========================
async def is_user_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    # نتحقق على القناة/المجموعة المربوطة للمشرف عبر bind_from_forward
    sess = admin_sessions.get(update.effective_user.id, {})
    target_channel_id = sess.get("target_channel_id")
    return await is_admin_in_chat(context, target_channel_id, update.effective_user.id)

async def start_publish(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # 🔒 منع تكرار شاشة الترحيب إن كانت جلسة نشر مفتوحة بالفعل
    existing = admin_sessions.get(user.id)
    if existing and existing.get("awaiting_input"):
        await update.message.reply_text("ℹ️ لديك جلسة نشر مفتوحة. أرسل المحتوى الآن أو اضغط ✅ تم.")
        return

    # تأكد أن المشرف ربط قناة/مجموعة عبر إعادة التوجيه
    sess = admin_sessions.get(user.id, {})
    target_channel_id = sess.get("target_channel_id")
    if not target_channel_id:
        await update.message.reply_text(
            "⚠️ قبل النشر: أعد توجيه أي منشور من القناة المراد النشر لها إلى الخاص هنا، لربطها (مرة واحدة فقط)."
        )
        return

    # إنشاء جلسة جديدة مباشرة — مع الاحتفاظ بالربط
    admin_sessions[user.id] = {
        "text": None,
        "media": None,
        "awaiting_input": True,
        "target_channel_id": target_channel_id,
        "controls_msg_id": None,
        "controls_chat_id": None,
    }

    await update.message.reply_text(
        text=(
            f"🧑‍💼 *المشرف:* `{user.full_name}`\n\n"
            "📝 *مرحبًا بك في نظام النشر.*\n"
            "✏️ أرسل *نص المنشور* أو وسائط:\n"
            "- صورة\n- فيديو\n- ملف\n- صوت\n\n"
            "✅ بعد الانتهاء اضغط زر *(تم)* لإرسال المنشور."
        ),
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("▶ Enter handle_text, current_reply=%s", context.bot_data.get("current_reply"))

    # لا نتدخل في جلسة الرد للمستخدم
    if context.bot_data.get("current_reply"):
        return

    msg = (update.message.text or "").strip()

    # 👇 تشغيل النشر بكتابة jop كنص (بدون /) وفق منطق الجلسة
    if re.fullmatch(r"\s*jop\s*", msg, flags=re.IGNORECASE):
        sess = admin_sessions.get(update.effective_user.id)
        if not sess or not sess.get("awaiting_input"):
            # لا توجد جلسة نشر مفتوحة → ابدأ جلسة نشر (start_publish سيتحقق من الربط أولاً)
            await start_publish(update, context)
        else:
            # جلسة نشر مفتوحة بالفعل → لا تعيد الترحيب
            await update.message.reply_text("ℹ️ لديك جلسة نشر مفتوحة. أرسل المحتوى الآن أو اضغط ✅ تم.")
        return

    # من هنا فصاعدًا: لا نتعامل إلا إذا كان المستخدم أدمن ومربوط بوجهة نشر
    if not await is_user_admin(update, context):
        return

    # لو الأدمن داخل جلسة استفسار كمستخدم → لا نتدخل
    inq = admin_inquiries.get(update.effective_user.id)
    if inq and inq.get("stage") == "awaiting_text_or_media":
        return

    session = admin_sessions.get(update.effective_user.id)
    if not session or not session.get("awaiting_input"):
        # لا توجد جلسة نشر مفتوحة، تجاهل أي نصوص عادية
        return

    # حفظ/تحديث نص المنشور
    session["text"] = auto_hide_links(msg)

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ تم", callback_data="admin_done_input")]])
    controls_msg_id = session.get("controls_msg_id")
    controls_chat_id = session.get("controls_chat_id")

    if not controls_msg_id:
        sent = await update.message.reply_text(
            "✍️ تم حفظ النص. يمكنك إضافة وسائط أو الضغط على ✅ تم",
            reply_markup=keyboard
        )
        session["controls_msg_id"] = sent.message_id
        session["controls_chat_id"] = sent.chat_id
    else:
        try:
            await context.bot.edit_message_text(
                chat_id=controls_chat_id,
                message_id=controls_msg_id,
                text="✍️ تم تحديث النص. يمكنك إضافة وسائط أو الضغط على ✅ تم",
                reply_markup=keyboard
            )
        except Exception:
            sent = await update.message.reply_text(
                "✍️ تم حفظ النص. يمكنك إضافة وسائط أو الضغط على ✅ تم",
                reply_markup=keyboard
            )
            session["controls_msg_id"] = sent.message_id
            session["controls_chat_id"] = sent.chat_id

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("▶ Enter handle_media, current_reply=%s", context.bot_data.get("current_reply"))

    if context.bot_data.get("current_reply"):
        return
    if not await is_user_admin(update, context):
        return
    inq = admin_inquiries.get(update.effective_user.id)
    if inq and inq.get("stage") == "awaiting_text_or_media":
        return

    session = admin_sessions.get(update.effective_user.id)
    if not session or not session.get("awaiting_input"):
        return

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("✅ تم", callback_data="admin_done_input")]])

    updated_label = None
    if update.message.photo:
        session["media"] = ("photo", update.message.photo[-1].file_id, update.message.caption)
        updated_label = "🖼️ تم حفظ الصورة. يمكنك إضافة نص أو الضغط على ✅ تم"
    elif update.message.document:
        session["media"] = ("document", update.message.document.file_id, update.message.caption)
        updated_label = "📎 تم حفظ الملف. يمكنك إضافة نص أو الضغط على ✅ تم"
    elif update.message.audio:
        session["media"] = ("audio", update.message.audio.file_id, update.message.caption)
        updated_label = "🎵 تم حفظ الملف الصوتي. يمكنك إضافة نص أو الضغط على ✅ تم"
    elif update.message.video:
        session["media"] = ("video", update.message.video.file_id, update.message.caption)
        updated_label = "🎬 تم حفظ الفيديو. يمكنك إضافة نص أو الضغط على ✅ تم"
    elif update.message.voice:
        session["media"] = ("voice", update.message.voice.file_id, None)
        updated_label = "🎙️ تم حفظ الرسالة الصوتية. يمكنك إضافة نص أو الضغط على ✅ تم"

    if not updated_label:
        return

    controls_msg_id = session.get("controls_msg_id")
    controls_chat_id = session.get("controls_chat_id")

    if not controls_msg_id:
        sent = await update.message.reply_text(updated_label, reply_markup=keyboard)
        session["controls_msg_id"] = sent.message_id
        session["controls_chat_id"] = sent.chat_id
    else:
        try:
            await context.bot.edit_message_text(
                chat_id=controls_chat_id,
                message_id=controls_msg_id,
                text=updated_label.replace("تم حفظ", "تم تحديث"),
                reply_markup=keyboard
            )
        except Exception:
            sent = await update.message.reply_text(updated_label, reply_markup=keyboard)
            session["controls_msg_id"] = sent.message_id
            session["controls_chat_id"] = sent.chat_id

async def handle_admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    # ✅ تحقق ديناميكي: هل هذا المستخدم أدمن في الوجهة المربوطة له؟
    sess = admin_sessions.get(user_id, {})
    target_channel_id = sess.get("target_channel_id")
    if not await is_admin_in_chat(context, target_channel_id, user_id):
        await query.answer("❌ اربط قناتك/مجموعتك أولًا بإعادة توجيه منشور منها للخاص.", show_alert=True)
        return

    session = admin_sessions[user_id]

    if data == "admin_done_input":
        session["awaiting_input"] = False
        session["use_reactions"] = None
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ نعم", callback_data="set_reactions_yes"),
                InlineKeyboardButton("❌ لا", callback_data="set_reactions_no")
            ]
        ])
        await query.message.reply_text(
            "❓ هل ترغب في إضافة أزرار التفاعل (إعجاب / لا يعجبني)؟",
            reply_markup=keyboard
        )
        await query.answer()

    elif data in ("set_reactions_yes", "set_reactions_no"):
        session["use_reactions"] = (data == "set_reactions_yes")
        preview_button = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ المعاينة", callback_data="preview_post")]
        ])
        msg = "😍 سيتم عرض أزرار التفاعل مع المنشور." if session["use_reactions"] else "✅ لن يتم عرض أزرار التفاعل."
        await query.message.reply_text(f"{msg}\nاضغط المعاينة للمتابعة:", reply_markup=preview_button)
        await query.answer()

    elif data == "preview_post":
        text = session.get("text")
        media = session.get("media")
        use_reactions = session.get("use_reactions")

        buttons = [
            [
                InlineKeyboardButton("✅ نشر", callback_data="confirm_publish"),
                InlineKeyboardButton("❌ إلغاء", callback_data="cancel_publish")
            ]
        ]
        if use_reactions:
            buttons.insert(0, [
                InlineKeyboardButton("😍 إعجاب", callback_data="none"),
                InlineKeyboardButton("😐 لا يعجبني", callback_data="none")
            ])
        keyboard = InlineKeyboardMarkup(buttons)

        if media:
            kind, file_id, caption = media
            send_args = {"reply_markup": keyboard, "parse_mode": "HTML", "caption": caption or text}
            if kind == "photo":
                await context.bot.send_photo(chat_id=user_id, photo=file_id, **send_args)
            elif kind == "document":
                await context.bot.send_document(chat_id=user_id, document=file_id, **send_args)
            elif kind == "audio":
                await context.bot.send_audio(chat_id=user_id, audio=file_id, **send_args)
            elif kind == "video":
                await context.bot.send_video(chat_id=user_id, video=file_id, **send_args)
            elif kind == "voice":
                await context.bot.send_voice(chat_id=user_id, voice=file_id, **send_args)
        elif text:
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )
        else:
            await query.message.reply_text("⚠️ لم يتم إدخال أي محتوى.")
        await query.answer()

    elif data == "confirm_publish":
        text = session.get("text")
        media = session.get("media")
        use_reactions = session.get("use_reactions")
        target_channel_id = session.get("target_channel_id")

        if not target_channel_id:
            await query.answer("⚠️ اربط قناتك/مجموعتك بإعادة توجيه منشور منها للخاص أولًا.", show_alert=True)
            return

        base_buttons = []
        if use_reactions:
            base_buttons.append([
                InlineKeyboardButton("😍 0", callback_data="like"),
                InlineKeyboardButton("😐  0", callback_data="dislike")
            ])
        keyboard = InlineKeyboardMarkup(base_buttons)

        sent_message = None
        send_args = {"reply_markup": keyboard, "parse_mode": "HTML"}

        if media:
            kind, file_id, caption = media
            send_args["caption"] = caption or text
            if kind == "photo":
                sent_message = await context.bot.send_photo(chat_id=target_channel_id, photo=file_id, **send_args)
            elif kind == "document":
                sent_message = await context.bot.send_document(chat_id=target_channel_id, document=file_id, **send_args)
            elif kind == "audio":
                sent_message = await context.bot.send_audio(chat_id=target_channel_id, audio=file_id, **send_args)
            elif kind == "video":
                sent_message = await context.bot.send_video(chat_id=target_channel_id, video=file_id, **send_args)
            elif kind == "voice":
                sent_message = await context.bot.send_voice(chat_id=target_channel_id, voice=file_id, **send_args)
        elif text:
            sent_message = await context.bot.send_message(
                chat_id=target_channel_id,
                text=text,
                reply_markup=keyboard,
                parse_mode="HTML"
            )

        # ✨ إضافة زر الملاحظة بعد النشر باستخدام chat_id الحقيقي + message_id
        if sent_message:
            bot_username = await get_bot_username(context)
            deep_link = f"https://t.me/{bot_username}?start=inq_{sent_message.chat_id}_{sent_message.message_id}"
            final_buttons = base_buttons + [[InlineKeyboardButton("💬 رفع ملاحظة للإدارة", url=deep_link)]]
            await context.bot.edit_message_reply_markup(
                chat_id=sent_message.chat_id,
                message_id=sent_message.message_id,
                reply_markup=InlineKeyboardMarkup(final_buttons)
            )

        # ✅ نحافظ على الربط ولا نمسحه — فقط نفرّغ حالة الجلسة
        binding = session.get("target_channel_id")
        admin_sessions[user_id] = {"target_channel_id": binding}

        await query.message.reply_text("✅ تم نشر المنشور بنجاح.")
        await query.answer()

    elif data == "cancel_publish":
        # إلغاء مع الحفاظ على الربط
        binding = session.get("target_channel_id")
        admin_sessions[user_id] = {"target_channel_id": binding}
        await query.message.reply_text("❌ تم إلغاء عملية النشر.")
        await query.answer()

# =========================
# تفاعلات القناة (ديناميكية)
# =========================
async def handle_reactions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    # نتأكد أن الضغط فقط على like / dislike
    if data not in ("like", "dislike"):
        await query.answer()
        return

    user_id = query.from_user.id
    chat_id = query.message.chat_id
    message_id = query.message.message_id

    # نقرأ الكيبورد الحالي من الرسالة
    markup = query.message.reply_markup
    if not markup or not markup.inline_keyboard:
        await query.answer("⚠️ لا يوجد أزرار تفاعل.", show_alert=True)
        return

    keyboard = markup.inline_keyboard

    # نفترض أول صف فيه الأزرار:
    # [ "😍 رقم", "😐 رقم" ]
    try:
        like_btn = keyboard[0][0]
        dislike_btn = keyboard[0][1]

        # نقرأ الأرقام من نهاية النص
        like_match = re.search(r"(\d+)\s*$", like_btn.text or "")
        dislike_match = re.search(r"(\d+)\s*$", dislike_btn.text or "")

        like_count = int(like_match.group(1)) if like_match else 0
        dislike_count = int(dislike_match.group(1)) if dislike_match else 0
    except Exception:
        like_count = 0
        dislike_count = 0

    # منع التصويت المكرر داخل نفس تشغيل السيرفر (اختياري)
    key = f"{chat_id}_{message_id}"
    reacted_map = context.bot_data.setdefault("reacted_users", {})
    reacted_set = reacted_map.setdefault(key, set())

    if user_id in reacted_set:
        await query.answer("لقد تفاعلت مسبقًا.", show_alert=True)
        return

    reacted_set.add(user_id)

    # نزيد العدّاد حسب نوع التفاعل
    if data == "like":
        like_count += 1
        msg = "تم تسجيل إعجابك 😍"
    else:
        dislike_count += 1
        msg = "تم تسجيل عدم إعجابك 😐 "

    bot_username = await get_bot_username(context)
    deep_link = f"https://t.me/{bot_username}?start=inq_{chat_id}_{message_id}"

    # نعيد بناء الأزرار بالأرقام الجديدة
    new_buttons = [
        [
            InlineKeyboardButton(f"😍 {like_count}", callback_data="like"),
            InlineKeyboardButton(f"😐  {dislike_count}", callback_data="dislike"),
        ],
        [
            InlineKeyboardButton("💬 رفع ملاحظة للإدارة", url=deep_link),
        ],
    ]

    try:
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(new_buttons))
    except Exception:
        # في حال فشل التعديل (رسالة قديمة مثلاً) نتجاهل الخطأ
        pass

    await query.answer(msg)

# =========================
# بدء محادثة المستخدم (/start) — التقاط الاستفسارات
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    args = context.args

    if chat.type != "private":
        return

    full_name = user.full_name

    # جاء من زر "رفع ملاحظة" (ديب لينك ديناميكي)
    if args and args[0].startswith("inq_"):
        try:
            _, raw_chat, raw_msg = args[0].split("_", 2)
            source_chat_id = int(raw_chat)
            post_message_id = int(raw_msg)
        except Exception:
            await update.message.reply_text("⚠️ رابط غير صالح. أعد المحاولة من زر المنشور.")
            return

        # منع التكرار لنفس المنشور
        user_records = context.bot_data.setdefault("inquiry_records", {})
        key = f"{user.id}_{post_message_id}"
        if post_message_id is not None and key in user_records:
            await update.message.reply_text(
                text=(
                    f"🧑‍💼 `{full_name}`\n\n"
                    "🚫 لقد قمت مسبقًا *بإرسال* ملاحظة على هذا المنشور.\n"
                    "لا يمكنك إرسال ملاحظة أخرى لنفس المنشور."
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # ابدأ جلسة كتابة
        admin_inquiries[user.id] = {
            "stage": "awaiting_text_or_media",
            "text": None,
            "media": None,
            "message_id": post_message_id,   # معرّف منشور المصدر
            "has_input": False,
            "confirm_msg_id": None,
            "confirm_chat_id": None,
            "source_chat_id": source_chat_id,  # 👈 محور العزل
        }

        await update.message.reply_text(
            text=(
                f"🧑‍💼 `{full_name}`\n\n"
                "🤝 أهلًا بك في مراسلة الإدارة.\n"
                "✏️ أرسل الآن ملاحظتك كنص أو وسائط:\n"
                "- صورة\n- فيديو\n- ملف\n- تسجيل صوتي\n\n"
                "بعد الانتهاء ستظهر لك أزرار (📤 إرسال) و(❌ إلغاء)."
            ),
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # فتح البوت بدون وسيط
    await update.message.reply_text(
        text=(
            f"🧑‍💼 `{full_name}`\n\n"
            "👋 أهلاً بك في نظام الدعم.\n"
            "للتواصل مع الإدارة، استخدم زر *(💬 رفع ملاحظة للإدارة)* أسفل أي منشور."
        ),
        parse_mode=ParseMode.MARKDOWN,
    )

# =========================
# التقاط محتوى المستخدم (نص/وسائط) للاستفسار
# =========================
async def handle_inquiry_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    user_id = update.effective_user.id

    if user_id not in admin_inquiries or admin_inquiries[user_id].get("stage") != "awaiting_text_or_media":
        return

    session = admin_inquiries[user_id]

    text = update.message.text
    caption = update.message.caption
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 إرسال", callback_data="send_inquiry")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_inquiry")]
    ])

    if text:
        session["text"] = auto_hide_links(text)
        await update.message.reply_text(
            "✍️ تم حفظ النص. يمكنك إضافة صورة / فيديو / ملف أو الضغط على 📤 إرسال.",
            reply_markup=keyboard
        )
        return
    elif caption and not session.get("text"):
        session["text"] = auto_hide_links(caption)

    if update.message.photo:
        session["media"] = ("photo", update.message.photo[-1].file_id, caption)
        await update.message.reply_text("🖼️ تم حفظ الصورة. يمكنك إضافة نص أو الضغط على 📤 إرسال.", reply_markup=keyboard)
    elif update.message.video:
        session["media"] = ("video", update.message.video.file_id, caption)
        await update.message.reply_text("🎬 تم حفظ الفيديو. يمكنك إضافة نص أو الضغط على 📤 إرسال.", reply_markup=keyboard)
    elif update.message.document:
        session["media"] = ("document", update.message.document.file_id, caption)
        await update.message.reply_text("📎 تم حفظ الملف. يمكنك إضافة نص أو الضغط على 📤 إرسال.", reply_markup=keyboard)
    elif update.message.audio:
        session["media"] = ("audio", update.message.audio.file_id, caption)
        await update.message.reply_text("🎵 تم حفظ الملف الصوتي. يمكنك إضافة نص أو الضغط على 📤 إرسال.", reply_markup=keyboard)
    elif update.message.voice:
        session["media"] = ("voice", update.message.voice.file_id, caption)
        await update.message.reply_text("🎙️ تم حفظ الرسالة الصوتية. يمكنك إضافة نص أو الضغط على 📤 إرسال.", reply_markup=keyboard)

# =========================
# أزرار تأكيد/إلغاء الاستفسار
# =========================
async def handle_inquiry_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data

    session = admin_inquiries.get(user_id)
    if not session or session.get("stage") not in ("awaiting_text_or_media", "preview"):
        await query.answer("لا توجد عملية نشطة.", show_alert=True)
        return

    post_message_id = session.get("message_id")
    lock_key = f"inq_send_lock:{user_id}:{post_message_id if post_message_id is not None else 'none'}"
    if context.bot_data.get(lock_key):
        await query.answer("جاري المعالجة…", show_alert=False)
        return

    async def _cleanup_ui():
        cmid = session.get("controls_msg_id")
        cchat = session.get("controls_chat_id")
        if cmid and cchat:
            try:
                await context.bot.edit_message_reply_markup(chat_id=cchat, message_id=cmid, reply_markup=None)
            except Exception:
                pass
        pmsg = session.get("preview_msg_id")
        pchat = session.get("preview_chat_id")
        if pmsg and pchat:
            try:
                await context.bot.edit_message_reply_markup(chat_id=pchat, message_id=pmsg, reply_markup=None)
            except Exception:
                pass
            try:
                await context.bot.delete_message(chat_id=pchat, message_id=pmsg)
            except Exception:
                pass

    if data == "send_inquiry":
        text = (session.get("text") or "").strip()
        media_list = session.get("media_list")
        if not media_list:
            single_media = session.get("media")
            media_list = [single_media] if single_media else []

        name = query.from_user.full_name
        uid = user_id

        if not text and not media_list:
            await query.answer("⚠️ لم يتم إدخال استفسار بعد.", show_alert=True)
            return

        if post_message_id is not None:
            records = context.bot_data.setdefault("inquiry_records", {})
            dup_key = f"{uid}_{post_message_id}"
            if records.get(dup_key):
                await query.answer("🚫 سبق وأرسلت استفسارًا لهذا المنشور.", show_alert=True)
                return

        try:
            context.bot_data[lock_key] = True

            inquiries = context.bot_data.setdefault("inquiries", {})
            inquiries[uid] = {
                "user_id": uid,
                "user_name": name,
                "text": text or None,
                "media_list": media_list,
                "status": "pending_send",
                "sent_at": datetime.now().isoformat(),
                "post_message_id": post_message_id,
                "source_chat_id": session.get("source_chat_id"),
            }

            await notify_admin_of_inquiry(context, uid)

            if post_message_id is not None:
                records = context.bot_data.setdefault("inquiry_records", {})
                records[f"{uid}_{post_message_id}"] = True

            inquiries[uid]["status"] = "sent"

            await _cleanup_ui()
            admin_inquiries.pop(user_id, None)

            await query.message.reply_text(
                "✅ تم إرسال ملاحظتك إلى ادارة القناة.\n📬 سيتم الرد عليك قريبًا.\n\n🤝 شكرًا لتواصلك معنا."
            )
            await query.answer()

        finally:
            context.bot_data.pop(lock_key, None)

    elif data == "cancel_inquiry":
        await _cleanup_ui()
        admin_inquiries.pop(user_id, None)
        await query.message.reply_text("❌ تم إلغاء الاستفسار.")
        await query.answer()

# =========================
# إشعار المشرفين (رسالة واحدة + زرّين)
# =========================
async def notify_admin_of_inquiry(context: ContextTypes.DEFAULT_TYPE, uid: int):
    inquiries = context.bot_data.setdefault("inquiries", {})
    record = inquiries.get(uid)
    if not record:
        logging.error(f"[inq] notify_admin_of_inquiry: no record for uid={uid}")
        return

    user_name      = record.get("user_name") or "غير معروف"
    user_id        = record.get("user_id") or uid
    source_chat_id = record.get("source_chat_id")
    text           = (record.get("text") or "").strip()
    media_list     = record.get("media_list") or []

    if not source_chat_id:
        logging.error("[inq] notify_admin_of_inquiry: لا يوجد source_chat_id.")
        return

    admin_ids = []
    try:
        admins = await context.bot.get_chat_administrators(source_chat_id)
        admin_ids = [m.user.id for m in admins if not m.user.is_bot]
    except Exception as e:
        logging.error(f"خطأ في جلب قائمة المشرفين ديناميكيًا: {e}")

    if not admin_ids:
        logging.error("[inq] notify_admin_of_inquiry: لا يوجد مشرفون.")
        return

    def keyboard(for_user_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 رد جاهز", callback_data=f"quick_reply|{for_user_id}")],
            [InlineKeyboardButton("✍️ رد مخصص", callback_data=f"custom_reply|{for_user_id}")]
        ])

    def safe_html(s: str | None) -> str:
        return auto_hide_links((s or "").strip())

    extra_count = max(0, len(media_list) - 1)
    extra_note = f"\n\n(+{extra_count} وسائط إضافية)" if extra_count > 0 else ""

    caption_html = (
        "<b>📥 ورد استفسار جديد</b>\n"
        f"👤 <b>المستخدم:</b> <code>{user_name}</code>\n"
        f"🆔 <b>المعرف:</b> <code>{user_id}</code>\n\n"
    )
    if text:
        caption_html += f"📝 <b>المحتوى:</b>\n{safe_html(text)}"
    else:
        caption_html += "📝 <b>المحتوى:</b> <i>بدون نص</i>"
    caption_html += extra_note

    for aid in admin_ids:
        try:
            if media_list:
                kind, file_id, _ = media_list[0]  # وسيط واحد فقط لربط الأزرار بالرسالة
                if kind == "photo":
                    await context.bot.send_photo(
                        chat_id=aid, photo=file_id, caption=caption_html,
                        parse_mode=ParseMode.HTML, reply_markup=keyboard(user_id)
                    )
                elif kind == "video":
                    await context.bot.send_video(
                        chat_id=aid, video=file_id, caption=caption_html,
                        parse_mode=ParseMode.HTML, reply_markup=keyboard(user_id)
                    )
                elif kind == "document":
                    await context.bot.send_document(
                        chat_id=aid, document=file_id, caption=caption_html,
                        parse_mode=ParseMode.HTML, reply_markup=keyboard(user_id)
                    )
                elif kind == "audio":
                    await context.bot.send_audio(
                        chat_id=aid, audio=file_id, caption=caption_html,
                        parse_mode=ParseMode.HTML, reply_markup=keyboard(user_id)
                    )
                elif kind == "voice":
                    await context.bot.send_voice(
                        chat_id=aid, voice=file_id, caption=caption_html,
                        parse_mode=ParseMode.HTML, reply_markup=keyboard(user_id)
                    )
                else:
                    await context.bot.send_message(
                        chat_id=aid, text=caption_html + "\n\n⚠️ نوع الوسائط غير مدعوم.",
                        parse_mode=ParseMode.HTML, reply_markup=keyboard(user_id)
                    )
            else:
                await context.bot.send_message(
                    chat_id=aid, text=caption_html,
                    parse_mode=ParseMode.HTML, reply_markup=keyboard(user_id)
                )
        except Exception as e:
            logging.error(f"فشل إرسال الاستفسار للمشرف {aid}: {e}")

# =========================
# ردود المشرفين (جاهز/مخصص) + حماية ديناميكية
# =========================
async def handle_quick_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data_parts = query.data.split("|")

    if len(data_parts) < 2 or not data_parts[1].isdigit():
        await query.answer("⚠️ لا يمكن تحديد المستخدم!", show_alert=True)
        return

    user_id = int(data_parts[1])

    # تحقق من أن الضاغط أدمن في نفس القناة/المجموعة الخاصة بالاستفسار
    rec = context.bot_data.setdefault("inquiries", {}).get(user_id)
    src = rec.get("source_chat_id") if rec else None
    if not await is_admin_in_chat(context, src, query.from_user.id):
        await query.answer("غير مخوّل لهذا الاستفسار.", show_alert=True)
        return

    quick_replies = [
        "📬 شكرًا لملاحظتك، تم إحالتها للفريق المختص للمراجعة.",
        "📌 تم استلام اقتراحك، وسيتم دراسته بعناية من قبل الإدارة.",
        "🤝 نقدر تواصلك، وتم رفع الملاحظة للجهة المعنية.",
        "📝 الملاحظة وصلت بوضوح، ونشكر اهتمامك.",
        "🧾 تم استلام استفسارك، وسيتم الرد بأقرب وقت من خلال القناة.",
        "✅ شكراً لاستفسارك، تمت معالجته وفق سياسة النشر المتبعة لدينا.",
        "🗂️ استفسارك مهم، وتم رفعه للمتابعة مع القسم المسؤول.",
        "🌟 شكراً لك على دعمك الجميل، هذا يُحفزنا لتقديم الأفضل.",
        "💙 نعتز بثقتك، ونأمل أن نكون دائمًا عند حسن الظن.",
        "📌 المعلومات المتعلقة بالوظائف والدورات تُنشر بشكل دوري في القناة فقط."
    ]

    context.bot_data["quick_reply_map"] = {
        f"{user_id}_{i}": {"target_user_id": user_id, "text": reply}
        for i, reply in enumerate(quick_replies)
    }

    buttons = [
        [InlineKeyboardButton(reply, callback_data=f"send_quick_reply|{user_id}_{i}")]
        for i, reply in enumerate(quick_replies)
    ]
    buttons.append([InlineKeyboardButton("❌ إلغاء", callback_data="cancel_reply")])

    await query.message.reply_text(
        "🗂️ اختر الرد الجاهز لإرساله:",
        reply_markup=InlineKeyboardMarkup(buttons)
    )
    await query.answer()

async def handle_send_quick_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    try:
        parts = query.data.split("|", 1)
        if len(parts) != 2:
            await query.answer("⚠️ تنسيق غير صالح!", show_alert=True)
            return

        key = parts[1]
        quick_map = context.bot_data.get("quick_reply_map", {})
        record = quick_map.get(key)

        if not record:
            await query.answer("⚠️ لم يتم العثور على الرد!", show_alert=True)
            return

        user_id = record["target_user_id"]

        # تحقق صلاحية المشرف
        rec = context.bot_data.setdefault("inquiries", {}).get(user_id)
        src = rec.get("source_chat_id") if rec else None
        if not await is_admin_in_chat(context, src, query.from_user.id):
            await query.answer("غير مخوّل لهذا الاستفسار.", show_alert=True)
            return

        reply_text = record["text"]
        context.bot_data["reply_payload"] = {"target_id": user_id, "text": reply_text.strip(), "media": None}
        context.bot_data["current_reply"] = {"admin_id": query.from_user.id, "target_user_id": user_id}

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📤 إرسال الرد", callback_data="send_custom_reply")],
            [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_reply")]
        ])

        await query.message.reply_text(
            f"📝 الرد المختار:\n\n{reply_text.strip()}\n\n✍️ يمكنك تعديله أو إرسال وسائط الآن، ثم اضغط 📤 للإرسال.",
            reply_markup=keyboard
        )
        await query.answer()

    except Exception as e:
        logging.error(f"❌ خطأ أثناء تجهيز الرد الجاهز: {e}")
        await query.answer("حدث خطأ أثناء المعالجة", show_alert=True)

async def handle_custom_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data_parts = query.data.split("|")

    if len(data_parts) < 2 or not data_parts[1].isdigit():
        await query.answer("⚠️ لا يمكن تحديد المستخدم!", show_alert=True)
        return

    target_user_id = int(data_parts[1])

    # تحقق صلاحية المشرف
    rec = context.bot_data.setdefault("inquiries", {}).get(target_user_id)
    src = rec.get("source_chat_id") if rec else None
    if not await is_admin_in_chat(context, src, query.from_user.id):
        await query.answer("غير مخوّل لهذا الاستفسار.", show_alert=True)
        return

    context.bot_data["current_reply"] = {
        "admin_id": query.from_user.id,
        "target_user_id": target_user_id
    }

    await query.message.reply_text("✍️ الرجاء كتابة الرد المخصص الآن...")
    await query.answer()

async def handle_admin_reply_content(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print("▶ Entered handle_admin_reply_content, current_reply =", context.bot_data.get("current_reply"))

    # الحدث بدون رسالة
    if not getattr(update, "message", None):
        return

    admin_id = update.effective_user.id

    # لو المشرف داخل جلسة استفسار كمستخدم
    inq = admin_inquiries.get(admin_id)
    if inq and inq.get("stage") == "awaiting_text_or_media":
        return

    current_reply = context.bot_data.get("current_reply")

    # لا توجد جلسة رد → قد تكون جلسة نشر
    if not current_reply:
        pub = admin_sessions.get(admin_id)
        if pub and pub.get("awaiting_input"):
            if update.message.text:
                await handle_text(update, context)
            elif any([update.message.photo, update.message.video, update.message.document, update.message.audio, update.message.voice]):
                await handle_media(update, context)
        return

    # استخراج target_id
    target_id = None
    if isinstance(current_reply, dict):
        target_id = (current_reply.get("target_user_id") or current_reply.get("target") or current_reply.get("user_id"))
    if not target_id:
        return

    # تحقق صلاحية المشرف قبل حفظ الرد
    rec = context.bot_data.setdefault("inquiries", {}).get(target_id)
    src = rec.get("source_chat_id") if rec else None
    if not await is_admin_in_chat(context, src, admin_id):
        return

    text = update.message.text
    caption = update.message.caption
    media = None

    previous = context.bot_data.get("reply_payload", {})
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 إرسال الرد", callback_data="send_custom_reply")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="cancel_reply")]
    ])

    if text:
        context.bot_data["reply_payload"] = {
            "target_id": target_id,
            "text": auto_hide_links(text),
            "media": previous.get("media")
        }
        await update.message.reply_text(
            "✍️ تم حفظ النص. أضف وسائط الآن أو اضغط 📤 للإرسال.",
            reply_markup=keyboard
        )
        return

    if update.message.photo:
        media = ("photo", update.message.photo[-1].file_id, caption)
        await update.message.reply_text("🖼️ تم حفظ الصورة. اكتب نصًا أو اضغط 📤 للإرسال.", reply_markup=keyboard)
    elif update.message.video:
        media = ("video", update.message.video.file_id, caption)
        await update.message.reply_text("🎬 تم حفظ الفيديو. اكتب نصًا أو اضغط 📤 للإرسال.", reply_markup=keyboard)
    elif update.message.document:
        media = ("document", update.message.document.file_id, caption)
        await update.message.reply_text("📎 تم حفظ الملف. اكتب نصًا أو اضغط 📤 للإرسال.", reply_markup=keyboard)
    elif update.message.audio:
        media = ("audio", update.message.audio.file_id, caption)
        await update.message.reply_text("🎵 تم حفظ الملف الصوتي. اكتب نصًا أو اضغط 📤 للإرسال.", reply_markup=keyboard)
    elif update.message.voice:
        media = ("voice", update.message.voice.file_id, None)
        await update.message.reply_text("🎙️ تم حفظ الرسالة الصوتية. اكتب نصًا أو اضغط 📤 للإرسال.", reply_markup=keyboard)

    if media:
        context.bot_data["reply_payload"] = {
            "target_id": target_id,
            "text": previous.get("text", caption if caption else ""),
            "media": media
        }

async def send_custom_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    payload = context.bot_data.pop("reply_payload", {})
    target_id = payload.get("target_id")

    if not target_id:
        await query.answer("لا توجد جلسة رد", show_alert=True)
        return

    # تحقق صلاحية المشرف
    rec = context.bot_data.setdefault("inquiries", {}).get(target_id)
    src = rec.get("source_chat_id") if rec else None
    if not await is_admin_in_chat(context, src, query.from_user.id):
        await query.answer("غير مخوّل لهذا الاستفسار.", show_alert=True)
        return

    admin_name = query.from_user.full_name
    admin_id = query.from_user.id
    text = payload.get("text", "")
    media = payload.get("media")

    intro_text = "📩 رد على مداخلتك من قبل إدارة القناة\n\n"
    outro_text = "\n\n🤝 شكرًا لتواصلك معنا."

    inquiries = context.bot_data.setdefault("inquiries", {})
    record = inquiries.get(target_id, {})
    handled_by = record.get("handled_by")
    handled_by_id = record.get("handled_by_id")

    # 🚫 منع الازدواجية + تنبيه باسم المشرف الذي رد مسبقًا
    if handled_by and handled_by_id != admin_id:
        await query.answer(f"تم الرد مسبقًا من قبل {handled_by}.", show_alert=True)
        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    try:
        # إرسال الرد للمستخدم
        if media:
            kind, file_id, caption = media
            caption = f"{intro_text}{caption or text or ''}{outro_text}"

            if kind == "photo":
                await context.bot.send_photo(chat_id=target_id, photo=file_id, caption=caption)
            elif kind == "video":
                await context.bot.send_video(chat_id=target_id, video=file_id, caption=caption)
            elif kind == "document":
                await context.bot.send_document(chat_id=target_id, document=file_id, caption=caption)
            elif kind == "audio":
                await context.bot.send_audio(chat_id=target_id, audio=file_id, caption=caption)
            elif kind == "voice":
                await context.bot.send_voice(chat_id=target_id, voice=file_id, caption=caption)
        else:
            caption = f"{intro_text}{text or ''}{outro_text}"
            await context.bot.send_message(chat_id=target_id, text=caption)

        # علّم الاستفسار كمُعالج (اسم المشرف محفوظ)
        record["handled_by"] = admin_name
        record["handled_by_id"] = admin_id
        record["handled_at"] = datetime.now().isoformat()
        inquiries[target_id] = record

        # إشعار مشرفي نفس القناة/المجموعة (ديناميكي)
        user_name = record.get("user_name", "غير معروف")
        user_text = record.get("text", "📎 وسائط فقط")
        notify_msg = (
            f"📢 *تم إرسال رد على استفسار:*\n"
            f"*👤 الاسم:* `{user_name}`\n"
            f"🆔 *المستخدم:* `{target_id}`\n"
            f"📝 *الاستفسار:*\n`{(user_text or '')[:100]}`\n\n"
            f"✍️ *الرد المرسل:*\n`{(text or '')[:100]}`\n\n"
            f"👨‍💼 *المشرف:* `{admin_name}`"
        )

        src_chat = record.get("source_chat_id")
        if src_chat:
            try:
                admins = await context.bot.get_chat_administrators(src_chat)
                admin_ids = [m.user.id for m in admins if not m.user.is_bot]
            except Exception:
                admin_ids = []
        else:
            admin_ids = []

        for aid in admin_ids:
            try:
                await context.bot.send_message(chat_id=aid, text=notify_msg, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logging.error(f"فشل إشعار المشرف {aid} بنتيجة الرد: {e}")

        try:
            await query.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

        await query.answer()
        context.bot_data.pop("current_reply", None)

    except Exception as e:
        logging.error(f"❌ خطأ أثناء إرسال الرد: {e}")
        await query.answer("حدث خطأ أثناء الإرسال", show_alert=True)

async def handle_reply_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("▶ Enter handle_reply_button: %s", update.callback_query.data)
    query = update.callback_query
    if not query.data.startswith("reply_"):
        return
    target_id = int(query.data.split("_", 1)[1])

    # تحقق صلاحية المشرف
    rec = context.bot_data.setdefault("inquiries", {}).get(target_id)
    src = rec.get("source_chat_id") if rec else None
    if not await is_admin_in_chat(context, src, query.from_user.id):
        await query.answer("غير مخوّل لهذا الاستفسار.", show_alert=True)
        return

    context.bot_data["current_reply"] = {"target": target_id}
    await query.message.reply_text("✍️ الرجاء الآن كتابة الرد:")
    await query.answer()

async def cancel_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.info("▶ Enter cancel_reply")
    query = update.callback_query
    admin_name = query.from_user.full_name

    current_reply = context.bot_data.pop("current_reply", None)
    if isinstance(current_reply, dict):
        user_id = current_reply.get("target")
        user_name = current_reply.get("user_name", "مستخدم غير معروف")
    else:
        user_id = current_reply
        user_name = "مستخدم غير معروف"

    await query.message.reply_text(
        text=(
            f"🚫 *تم إلغاء المداخلة الخاصة بالمستخدم التالي:*\n"
            f"🧑‍💼 `{user_name}`\n\n"
            f"❎ *تم الإلغاء بواسطة:*\n"
            f"`{admin_name}`"
        ),
        parse_mode=ParseMode.MARKDOWN
    )
    await query.answer()

# =========================
# بناء تطبيق تيليجرام وتسجيل الهاندلرات (عالميًا)
# =========================
application = ApplicationBuilder().token(TOKEN).build()

# أوامر
application.add_handler(CommandHandler("start", start), group=0)
application.add_handler(CommandHandler("jop", handle_jop_command), group=0)

application.add_handler(CommandHandler("webhookinfo", webhookinfo), group=0)
application.add_handler(CommandHandler("status", status_cmd), group=0)
application.add_handler(CommandHandler("reset", reset_publish), group=0)

# ربط الوجهة عبر إعادة توجيه (خاص)
# يصير:
application.add_handler(CommandHandler("bind", bind_by_username), group=0)
application.add_handler(MessageHandler(filters.ChatType.PRIVATE, bind_from_forward), group=0)

# 🟢 رسائل المستخدم (الاستفسار)
application.add_handler(MessageHandler(
    filters.ChatType.PRIVATE
    & ~filters.COMMAND
    & (filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.AUDIO | filters.VIDEO | filters.VOICE),
    handle_inquiry_input
), group=1)

# 🟠 محتوى ردود الأدمن — داخل الخاص فقط
application.add_handler(MessageHandler(
    filters.ChatType.PRIVATE
    & (filters.TEXT | filters.PHOTO | filters.Document.ALL | filters.AUDIO | filters.VIDEO | filters.VOICE),
    handle_admin_reply_content
), group=2)

# ✏️ مدخلات تجهيز منشور الأدمن — داخل الخاص فقط
application.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.TEXT & ~filters.COMMAND), handle_text), group=3)
application.add_handler(MessageHandler(
    filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.ALL | filters.AUDIO | filters.VIDEO | filters.VOICE),
    handle_media
), group=3)

# 🔔 أزرار المستخدم أولاً
application.add_handler(CallbackQueryHandler(handle_inquiry_buttons, pattern="^(send_inquiry|cancel_inquiry)$"), group=4)

# 🛠️ أزرار الأدمن
application.add_handler(CallbackQueryHandler(
    handle_admin_buttons,
    pattern="^(admin_done_input|set_reactions_yes|set_reactions_no|preview_post|confirm_publish|cancel_publish)$"
), group=4)

# ردود الأدمن (جاهز/مخصص)
application.add_handler(CallbackQueryHandler(handle_reply_button, pattern="^reply_"), group=5)
application.add_handler(CallbackQueryHandler(cancel_reply, pattern="^cancel_reply$"), group=5)
application.add_handler(CallbackQueryHandler(handle_quick_reply, pattern="^quick_reply\\|"), group=5)
application.add_handler(CallbackQueryHandler(handle_send_quick_reply, pattern="^send_quick_reply\\|"), group=5)
application.add_handler(CallbackQueryHandler(handle_custom_reply, pattern="^custom_reply\\|"), group=5)
application.add_handler(CallbackQueryHandler(send_custom_reply, pattern="^send_custom_reply$"), group=5)

# تفاعلات
application.add_handler(CallbackQueryHandler(handle_reactions, pattern="^(like|dislike)$"), group=6)

# =========================
# FastAPI (لـ Render Web Service)
# =========================
app = FastAPI()

# صفحة رئيسية بسيطة لتفادي 404 عند فتح الدومين مباشرة
@app.get("/")
async def root():
    return PlainTextResponse("Bot is running. Use /health for uptime checks.")

# فحص الصحة لـ UptimeRobot (GET)
@app.get("/health")
async def health():
    return PlainTextResponse("ok")

# دعم HEAD لـ /health لأن بعض أدوات المراقبة تستخدم HEAD
@app.head("/health")
async def health_head():
    return PlainTextResponse("ok")

@app.on_event("startup")
async def on_startup():
    await application.initialize()
    await application.start()

    if not APP_URL:
        logging.warning("APP_URL (PUBLIC_URL/RENDER_EXTERNAL_URL) not set yet. Restart later to set webhook.")
        return

    webhook_path = f"/webhook/{WEBHOOK_SECRET}"
    webhook_url = f"{APP_URL}{webhook_path}"
    await application.bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET)
    logging.info("Webhook set to: %s", webhook_url)

@app.on_event("shutdown")
async def on_shutdown():
    try:
        await application.bot.delete_webhook()
    except Exception:
        pass
    await application.stop()
    await application.shutdown()

# Webhook الحقيقي (POST فقط من تيليجرام)
@app.post(f"/webhook/{{secret}}")
async def telegram_webhook(secret: str, request: Request):
    if secret != WEBHOOK_SECRET:
        return PlainTextResponse("forbidden", status_code=403)
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return PlainTextResponse("ok")

# (اختياري) تمكين GET على مسار الويبهوك لتجنّب 405 إذا انضبط في UptimeRobot بالخطأ
@app.get(f"/webhook/{{secret}}")
async def webhook_probe(secret: str):
    if secret != WEBHOOK_SECRET:
        return PlainTextResponse("forbidden", status_code=403)
    return PlainTextResponse("ok")