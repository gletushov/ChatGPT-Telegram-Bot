import re
import sys
sys.dont_write_bytecode = True
import logging
import traceback
import utils.decorators as decorators

from md2tgmd.src.md2tgmd import escape, split_code, replace_all
from ModelMerge.src.ModelMerge.utils.prompt import translator_en2zh_prompt, translator_prompt
from ModelMerge.src.ModelMerge.utils.scripts import Document_extract, claude_replace, get_image_message

import config
from config import (
    WEB_HOOK,
    PORT,
    BOT_TOKEN,
    GOOGLE_AI_API_KEY,
    Users,
    PREFERENCES,
    LANGUAGES,
    PLUGINS,
    update_first_buttons_message,
    get_current_lang,
    update_info_message,
    update_ENGINE,
    reset_ENGINE,
    get_robot,
    get_ENGINE,
    update_language_status,
    update_models_buttons,
    update_menu_buttons,
)

from utils.i18n import strings
from utils.scripts import GetMesageInfo

from telegram.constants import ChatAction
from telegram import BotCommand, InlineKeyboardMarkup, InlineQueryResultArticle, InputTextMessageContent, Update, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from telegram.ext import CommandHandler, MessageHandler, ApplicationBuilder, filters, CallbackQueryHandler, Application, AIORateLimiter, InlineQueryHandler

import asyncio
lock = asyncio.Lock()
event = asyncio.Event()
stop_event = asyncio.Event()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger()

httpx_logger = logging.getLogger("httpx")
httpx_logger.setLevel(logging.CRITICAL)

httpx_logger = logging.getLogger("chromadb.telemetry.posthog")
httpx_logger.setLevel(logging.WARNING)

class SpecificStringFilter(logging.Filter):
    def __init__(self, specific_string):
        super().__init__()
        self.specific_string = specific_string

    def filter(self, record):
        return self.specific_string not in record.getMessage()

specific_string = "httpx.RemoteProtocolError: Server disconnected without sending a response."
my_filter = SpecificStringFilter(specific_string)

update_logger = logging.getLogger("telegram.ext.Updater")
update_logger.addFilter(my_filter)
update_logger = logging.getLogger("root")
update_logger.addFilter(my_filter)

# Define a global variable to store the system prompt
system_prompt = "Initial system prompt"

# 定义一个缓存来存储消息
from collections import defaultdict
message_cache = defaultdict(lambda: [])
time_stamps = defaultdict(lambda: [])

@decorators.PrintMessage
@decorators.GroupAuthorization
@decorators.Authorization
@decorators.APICheck

async def command_bot(update, context, language=None, prompt=translator_prompt, title="", has_command=True):
    stop_event.clear()
    message, rawtext, image_url, chatid, messageid, reply_to_message_text, update_message, message_thread_id, convo_id, file_url, reply_to_message_file_content, voice_text = await GetMesageInfo(update, context)

    if has_command == False or len(context.args) > 0:
        if has_command:
            message = ' '.join(context.args)
        pass_history = Users.get_config(convo_id, "PASS_HISTORY")
        if prompt and has_command:
            if translator_prompt == prompt:
                if language == "english":
                    prompt = prompt.format(language)
                else:
                    prompt = translator_en2zh_prompt
                pass_history = False
            message = prompt + message
        if message == None:
            message = voice_text
        if message:
            bot_info = await context.bot.get_me()
            if update_message.reply_to_message \
            and update_message.from_user.is_bot == False \
            and update_message.reply_to_message.from_user.username == bot_info.username:
                if update_message.reply_to_message.from_user.is_bot and Users.get_config(convo_id, "TITLE") == True:
                    message = '\n'.join(reply_to_message_text.split('\n')[1:]) + "\n" + message
                else:
                    if reply_to_message_text:
                        message = reply_to_message_text + "\n" + message
                    if reply_to_message_file_content:
                        message = reply_to_message_file_content + "\n" + message
            elif update_message.reply_to_message and update_message.reply_to_message.from_user.is_bot \
            and update_message.reply_to_message.from_user.username != bot_info.username:
                return

            robot, role = get_robot(convo_id)
            engine = get_ENGINE(convo_id)

            if Users.get_config(convo_id, "LONG_TEXT"):
                async with lock:
                    message_cache[convo_id].append(message)
                    import time
                    time_stamps[convo_id].append(time.time())
                    if len(message_cache[convo_id]) == 1:
                        print("first message len:", len(message_cache[convo_id][0]))
                        if len(message_cache[convo_id][0]) > 800:
                            event.clear()
                        else:
                            event.set()
                    else:
                        return
                try:
                    await asyncio.wait_for(event.wait(), timeout=2)
                except asyncio.TimeoutError:
                    print("asyncio.wait timeout!")

                intervals = [
                    time_stamps[convo_id][i] - time_stamps[convo_id][i - 1]
                    for i in range(1, len(time_stamps[convo_id]))
                ]
                print(f"Chat ID {convo_id} 时间间隔: {intervals}，总时间：{sum(intervals)}")

                message = "\n".join(message_cache[convo_id])
                message_cache[convo_id] = []
                time_stamps[convo_id] = []
            if Users.get_config(convo_id, "TYPING"):
                await context.bot.send_chat_action(chat_id=chatid, message_thread_id=message_thread_id, action=ChatAction.TYPING)
            if Users.get_config(convo_id, "TITLE"):
                title = f"`🤖️ {engine}`\n\n"
            if Users.get_config(convo_id, "REPLY") == False:
                messageid = None

            if image_url:
                if "gemini" in engine and GOOGLE_AI_API_KEY:
                    message = get_image_message(image_url, [{"text": message}], engine)
                else:
                    message = get_image_message(image_url, [{"type": "text", "text": message}], engine)
            await getChatGPT(update, context, title, robot, message, chatid, messageid, convo_id, message_thread_id, pass_history)
    else:
        message = await context.bot.send_message(
            chat_id=chatid,
            message_thread_id=message_thread_id,
            text=escape(strings['message_command_text_none'][get_current_lang(convo_id)]),
            parse_mode='MarkdownV2',
            reply_to_message_id=messageid,
        )

async def delete_message(update, context, messageid = [], delay=60):
    await asyncio.sleep(delay)
    if isinstance(messageid, list):
        for mid in messageid:
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=mid)
            except Exception as e:
                print('\033[31m')
                print("delete_message error", e)
                print('\033[0m')

async def getChatGPT(update, context, title, robot, message, chatid, messageid, convo_id, message_thread_id, pass_history=False):
    lastresult = title
    text = message
    result = ""
    tmpresult = ""
    modifytime = 0
    time_out = 600
    image_has_send = 0
    model_name = Users.get_config(convo_id, "engine")
    language = Users.get_config(convo_id, "language")

    Frequency_Modification = 20
    if "gpt-4o" in model_name:
        Frequency_Modification = 25
    if message_thread_id or convo_id.startswith("-"):
        Frequency_Modification = 35
    if "gemini" in model_name and GOOGLE_AI_API_KEY:
        Frequency_Modification = 1


    answer_messageid = (await context.bot.send_message(
        chat_id=chatid,
        message_thread_id=message_thread_id,
        text=escape(strings['message_think'][get_current_lang(convo_id)]),
        parse_mode='MarkdownV2',
        reply_to_message_id=messageid,
    )).message_id

    try:
        async for data in robot.ask_stream(text, convo_id=convo_id, pass_history=pass_history, model=model_name, language=language):
        # for data in robot.ask_stream(text, convo_id=convo_id, pass_history=pass_history, model=model_name):
            if stop_event.is_set() and convo_id == target_convo_id and answer_messageid < reset_mess_id:
                return
            if "🌐" not in data:
                result = result + data
            tmpresult = result
            if re.sub(r"```", '', result.split("\n")[-1]).count("`") % 2 != 0:
                tmpresult = result + "`"
            if sum([line.strip().startswith("```") for line in result.split('\n')]) % 2 != 0:
                tmpresult = tmpresult + "\n```"
            tmpresult = title + tmpresult
            if "claude" in model_name:
                tmpresult = claude_replace(tmpresult)
            if "🌐" in data:
                search_index_string = data.split(" ")[1]
                tmpresult = strings[search_index_string][get_current_lang(convo_id)]
            history = robot.conversation[convo_id]
            if history[-1].get('name') == "generate_image" and not image_has_send:
                await context.bot.send_photo(chat_id=chatid, photo=history[-1]['content'], reply_to_message_id=messageid)
                image_has_send = 1
            elif len(history) > 2 \
            and type(history[-2]) != str \
            and history[-2].get('content') \
            and type(history[-2].get('content')[0]) != str \
            and history[-2].get('content')[0].get("name") == "generate_image" \
            and not image_has_send:
                await context.bot.send_photo(chat_id=chatid, photo=history[-1]['content'][0]["content"], reply_to_message_id=messageid)
                image_has_send = 1
            modifytime = modifytime + 1

            if len(tmpresult) > 3500 and Users.get_config(convo_id, "LONG_TEXT_SPLIT"):
                # print("tmpresult", tmpresult)
                replace_text = replace_all(tmpresult, r"(```[\D\d\s]+?```)", split_code)
                if "@|@|@|@" in replace_text:
                    print("@|@|@|@", replace_text)
                    split_messages = replace_text.split("@|@|@|@")
                    send_split_message = split_messages[0]
                    result = split_messages[1][:-4]
                else:
                    print("replace_text", replace_text)
                    if replace_text.strip().endswith("```"):
                        replace_text = replace_text.strip()[:4]
                    split_messages_new = []
                    split_messages = replace_text.split("```")
                    for index, item in enumerate(split_messages):
                        if index % 2 == 1:
                            item = "```" + item
                            if index != len(split_messages) - 1:
                                item = item + "```"
                            split_messages_new.append(item)
                        if index % 2 == 0:
                            item_split_new = []
                            item_split = item.split("\n\n")
                            for sub_index, sub_item in enumerate(item_split):
                                if sub_index % 2 == 1:
                                    sub_item = "\n\n" + sub_item
                                    if sub_index != len(item_split) - 1:
                                        sub_item = sub_item + "\n\n"
                                    item_split_new.append(sub_item)
                                if sub_index % 2 == 0:
                                    item_split_new.append(sub_item)
                            split_messages_new.extend(item_split_new)

                    split_index = 0
                    for index, _ in enumerate(split_messages_new):
                        if len("".join(split_messages_new[:index])) < len(text) // 2:
                            split_index += 1
                            continue
                        else:
                            break
                    send_split_message = ''.join(split_messages_new[:split_index])
                    tmp = ''.join(split_messages_new[split_index:])
                    if not tmp.strip().endswith("```"):
                        result = tmp[:4]
                    else:
                        result = tmp

                await context.bot.edit_message_text(
                    chat_id=chatid,
                    message_id=answer_messageid,
                    text=escape(send_split_message, italic=False),
                    parse_mode='MarkdownV2',
                    disable_web_page_preview=True,
                    read_timeout=time_out,
                    write_timeout=time_out,
                    pool_timeout=time_out,
                    connect_timeout=time_out
                )
                answer_messageid = (await context.bot.send_message(
                    chat_id=chatid,
                    message_thread_id=message_thread_id,
                    text=escape(strings['message_think'][get_current_lang(convo_id)]),
                    parse_mode='MarkdownV2',
                    reply_to_message_id=messageid,
                )).message_id

            now_result = escape(tmpresult, italic=False)
            if now_result and (modifytime % Frequency_Modification == 0 and lastresult != now_result) or "🌐" in data:
                lastresult = now_result
                await context.bot.edit_message_text(chat_id=chatid, message_id=answer_messageid, text=now_result, parse_mode='MarkdownV2', disable_web_page_preview=True, read_timeout=time_out, write_timeout=time_out, pool_timeout=time_out, connect_timeout=time_out)
    except Exception as e:
        print('\033[31m')
        traceback.print_exc()
        print(tmpresult)
        print('\033[0m')
        api_key = Users.get_config(convo_id, "api_key")
        systemprompt = Users.get_config(convo_id, "systemprompt")
        if api_key:
            robot.reset(convo_id=convo_id, system_prompt=systemprompt)
        tmpresult = f"{tmpresult}\n\n`{e}`"
    print(tmpresult)
    now_result = escape(tmpresult, italic=False)
    if lastresult != now_result and answer_messageid:
        if "Can't parse entities: can't find end of code entity at byte offset" in tmpresult:
            await update.message.reply_text(tmpresult)
            print(now_result)
        elif now_result:
            sent_message = await context.bot.edit_message_text(chat_id=chatid, message_id=answer_messageid, text=now_result, parse_mode='MarkdownV2', disable_web_page_preview=True, read_timeout=time_out, write_timeout=time_out, pool_timeout=time_out, connect_timeout=time_out)

    if Users.get_config(convo_id, "FOLLOW_UP"):
        if title != "":
            info = "\n\n".join(tmpresult.split("\n\n")[1:])
        else:
            info = tmpresult
        prompt = (
            f"You are a professional Q&A expert. You will now be given reference information. Based on the reference information, please help me ask three most relevant questions that you most want to know from my perspective. Be concise and to the point. Do not have numbers in front of questions. Separate each question with a line break. Only output three questions in {language}, no need for any explanation. reference infomation is provided inside <infomation></infomation> XML tags."
            "Here is the reference infomation, inside <infomation></infomation> XML tags:"
            "<infomation>"
            "{}"
            "</infomation>"
        ).format(info)
        result = (await config.SummaryBot.ask(prompt, convo_id=convo_id, pass_history=False)).split('\n')
        keyboard = []
        result = [i for i in result if i.strip() and len(i) > 5]
        print(result)
        for ques in result:
            keyboard.append([KeyboardButton(ques)])
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(text=escape(tmpresult, italic=False), parse_mode='MarkdownV2', reply_to_message_id=messageid, reply_markup=reply_markup)
        await context.bot.delete_message(chat_id=chatid, message_id=sent_message.message_id)

@decorators.AdminAuthorization
@decorators.GroupAuthorization
@decorators.Authorization
async def button_press(update, context):
    """Function to handle the button press"""
    _, rawtext, _, _, _, _, _, _, convo_id, _, _, voice_text = await GetMesageInfo(update, context)
    callback_query = update.callback_query
    info_message = update_info_message(convo_id)
    await callback_query.answer()
    data = callback_query.data
    banner = strings['message_banner'][get_current_lang(convo_id)]

    if data.endswith("_MODELS"):
        data = data[:-7]
        update_ENGINE(data, convo_id)
        try:
            info_message = update_info_message(convo_id)
            if  info_message + banner != rawtext:
                message = await callback_query.edit_message_text(
                    text=escape(info_message + banner),
                    reply_markup=InlineKeyboardMarkup(update_models_buttons(convo_id)),
                    parse_mode='MarkdownV2'
                )
        except Exception as e:
            logger.info(e)
            pass
    elif data.startswith("MODELS"):
        message = await callback_query.edit_message_text(
            text=escape(info_message + banner),
            reply_markup=InlineKeyboardMarkup(update_models_buttons(convo_id)),
            parse_mode='MarkdownV2'
        )

    elif data.endswith("_LANGUAGES"):
        data = data[:-10]
        update_language_status(data, chat_id=convo_id)
        try:
            info_message = update_info_message(convo_id)
            if  info_message != rawtext:
                message = await callback_query.edit_message_text(
                    text=escape(info_message, italic=False),
                    reply_markup=InlineKeyboardMarkup(update_menu_buttons(LANGUAGES, "_LANGUAGES", convo_id)),
                    parse_mode='MarkdownV2'
                )
        except Exception as e:
            logger.info(e)
            pass
    elif data.startswith("LANGUAGE"):
        message = await callback_query.edit_message_text(
            text=escape(info_message, italic=False),
            reply_markup=InlineKeyboardMarkup(update_menu_buttons(LANGUAGES, "_LANGUAGES", convo_id)),
            parse_mode='MarkdownV2'
        )

    if data.endswith("_PREFERENCES"):
        data = data[:-12]
        try:
            current_data = Users.get_config(convo_id, data)
            Users.set_config(convo_id, data, not current_data)
        except Exception as e:
            logger.info(e)
        try:
            info_message = update_info_message(convo_id)
            if  info_message != rawtext:
                message = await callback_query.edit_message_text(
                    text=escape(info_message, italic=False),
                    reply_markup=InlineKeyboardMarkup(update_menu_buttons(PREFERENCES, "_PREFERENCES", convo_id)),
                    parse_mode='MarkdownV2'
                )
        except Exception as e:
            logger.info(e)
            pass
    elif data.startswith("PREFERENCES"):
        message = await callback_query.edit_message_text(
            text=escape(info_message, italic=False),
            reply_markup=InlineKeyboardMarkup(update_menu_buttons(PREFERENCES, "_PREFERENCES", convo_id)),
            parse_mode='MarkdownV2'
        )

    if data.endswith("_PLUGINS"):
        data = data[:-8]
        try:
            current_data = Users.get_config(convo_id, data)
            Users.set_config(convo_id, data, not current_data)
            plugins_config = Users.extract_plugins_config(convo_id)
            robot, role = get_robot(convo_id)
            if robot:
                robot.plugins[convo_id] = plugins_config
        except Exception as e:
            logger.info(e)
        try:
            info_message = update_info_message(convo_id)
            if  info_message != rawtext:
                message = await callback_query.edit_message_text(
                    text=escape(info_message, italic=False),
                    reply_markup=InlineKeyboardMarkup(update_menu_buttons(PLUGINS, "_PLUGINS", convo_id)),
                    parse_mode='MarkdownV2'
                )
        except Exception as e:
            logger.info(e)
            pass
    elif data.startswith("PLUGINS"):
        message = await callback_query.edit_message_text(
            text=escape(info_message, italic=False),
            reply_markup=InlineKeyboardMarkup(update_menu_buttons(PLUGINS, "_PLUGINS", convo_id)),
            parse_mode='MarkdownV2'
        )

    elif data.startswith("BACK"):
        message = await callback_query.edit_message_text(
            text=escape(info_message, italic=False),
            reply_markup=InlineKeyboardMarkup(update_first_buttons_message(convo_id)),
            parse_mode='MarkdownV2'
        )

@decorators.GroupAuthorization
@decorators.Authorization
@decorators.APICheck
async def handle_file(update, context):
    _, _, image_url, chatid, _, _, _, message_thread_id, convo_id, file_url, _, voice_text = await GetMesageInfo(update, context)
    robot, role = get_robot(convo_id)
    engine = get_ENGINE(convo_id)

    if file_url == None and image_url:
        file_url = image_url
        if Users.get_config(convo_id, "IMAGEQA") == False:
            return
    if image_url == None and file_url:
        image_url = file_url
    message = Document_extract(file_url, image_url, engine)

    robot.add_to_conversation(message, role, convo_id)

    if Users.get_config(convo_id, "FILE_UPLOAD_MESS"):
        message = await context.bot.send_message(chat_id=chatid, message_thread_id=message_thread_id, text=escape(strings['message_doc'][get_current_lang(convo_id)]), parse_mode='MarkdownV2', disable_web_page_preview=True)
        await delete_message(update, context, [message.message_id])

@decorators.GroupAuthorization
@decorators.Authorization
@decorators.APICheck
async def inlinequery(update: Update, context) -> None:
    """Handle the inline query."""

    chatid = update.effective_user.id
    engine = get_ENGINE(chatid)
    query = update.inline_query.query
    if (query.endswith('.') or query.endswith('。')) and query.strip():
        prompt = "Answer the following questions as concisely as possible:\n\n"
        result = config.ChatGPTbot.ask(prompt + query, convo_id=chatid, pass_history=False)

        results = [
            InlineQueryResultArticle(
                id=chatid,
                title=f"{engine}",
                thumbnail_url="https://pb.yym68686.top/TTGk",
                description=f"{result}",
                input_message_content=InputTextMessageContent(escape(result, italic=False), parse_mode='MarkdownV2')),
        ]

        await update.inline_query.answer(results)

# 定义一个全局变量来存储 chatid
target_convo_id = None
reset_mess_id = 9999

@decorators.GroupAuthorization
@decorators.Authorization
async def reset_chat(update, context):
    global target_convo_id, reset_mess_id
    _, _, _, chatid, user_message_id, _, _, message_thread_id, convo_id, _, _, _ = await GetMesageInfo(update, context)
    reset_mess_id = user_message_id
    target_convo_id = convo_id
    stop_event.set()
    message = None
    if (len(context.args) > 0):
        message = ' '.join(context.args)
    reset_ENGINE(target_convo_id, message)

    remove_keyboard = ReplyKeyboardRemove()
    message = await context.bot.send_message(
        chat_id=chatid,
        message_thread_id=message_thread_id,
        text=escape(strings['message_reset'][get_current_lang(convo_id)]),
        reply_markup=remove_keyboard,
        parse_mode='MarkdownV2',
    )
    await delete_message(update, context, [message.message_id, user_message_id])

@decorators.AdminAuthorization
@decorators.GroupAuthorization
@decorators.Authorization
async def info(update, context):
    _, _, _, chatid, user_message_id, _, _, message_thread_id, convo_id, _, _, voice_text = await GetMesageInfo(update, context)
    info_message = update_info_message(convo_id)
    message = await context.bot.send_message(
        chat_id=chatid,
        message_thread_id=message_thread_id,
        text=escape(info_message, italic=False),
        reply_markup=InlineKeyboardMarkup(update_first_buttons_message(convo_id)),
        parse_mode='MarkdownV2',
        disable_web_page_preview=True,
        read_timeout=600,
    )
    await delete_message(update, context, [message.message_id, user_message_id])

@decorators.PrintMessage
async def start(update, context): # 当用户输入/start时，返回文本
    _, _, _, _, _, _, _, _, convo_id, _, _, _ = await GetMesageInfo(update, context)
    user = update.effective_user
    if user.language_code == "zh-hans":
        update_language_status("Simplified Chinese", chat_id=convo_id)
    elif user.language_code == "zh-hant":
        update_language_status("Traditional Chinese", chat_id=convo_id)
    elif user.language_code == "ru":
        update_language_status("Russian", chat_id=convo_id)
    else:
        update_language_status("English", chat_id=convo_id)
    message = (
        f"Hi `{user.username}` ! I am an Assistant, a large language model trained by _OpenAI_. I will do my best to help answer your questions.\n\n"
        # "Welcome to visit https://github.com/yym68686/ChatGPT-Telegram-Bot to view the source code.\n\n"
        # "If you find any bugs, you can contact @yym68686."
    )
    if (len(context.args) == 2):
        api_url = context.args[0]
        api_key = context.args[1]
        Users.set_config(convo_id, "api_key", api_key)
        Users.set_config(convo_id, "api_url", api_url)
        update_ENGINE(chat_id=convo_id)

    if (len(context.args) == 1):
        api_key = context.args[0]
        Users.set_config(convo_id, "api_key", api_key)
        Users.set_config(convo_id, "api_url", "https://api.openai.com/v1/chat/completions")
        update_ENGINE(chat_id=convo_id)

    await update.message.reply_text(escape(message, italic=False), parse_mode='MarkdownV2', disable_web_page_preview=True)

async def error(update, context):
    traceback_string = traceback.format_exception(None, context.error, context.error.__traceback__)
    if "telegram.error.TimedOut: Timed out" in traceback_string:
        logger.warning('telegram.error.TimedOut: Timed out')
        return
    logger.warning('Update "%s" caused error "%s"', update, context.error)
    logger.warning('Error traceback: %s', ''.join(traceback_string))

@decorators.GroupAuthorization
@decorators.Authorization
async def unknown(update, context): # 当用户输入未知命令时，返回文本
    return
    # await context.bot.send_message(chat_id=update.effective_chat.id, text="Sorry, I didn't understand that command.")

async def update_system_prompt(update: Update, context: CallbackContext) -> None:
    """Handler for updating the system prompt."""
    global system_prompt
    try:
        new_prompt = ' '.join(context.args)
        if new_prompt:
            system_prompt = new_prompt
            await update.message.reply_text(f"System prompt updated to: {system_prompt}")
        else:
            await update.message.reply_text("Please provide a new prompt.")
    except Exception as e:
        logger.error(f"Error updating system prompt: {e}")
        await update.message.reply_text(f"Error updating system prompt: {e}")

async def post_init(application: Application) -> None:
    await application.bot.set_my_commands([
        BotCommand('info', 'Basic information'),
        BotCommand('reset', 'Reset the bot'),
        BotCommand('start', 'Start the bot'),
        BotCommand('en2zh', 'Translate to Chinese'),
        BotCommand('zh2en', 'Translate to English'),
        BotCommand('updateprompt', 'Update the system prompt'),
    ])
    description = (
        "I am an Assistant, a large language model trained by OpenAI. I will do my best to help answer your questions."
    )
    await application.bot.set_my_description(description)

if __name__ == '__main__':
    time_out = 600
    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .connection_pool_size(65536)
        .get_updates_connection_pool_size(65536)
        .read_timeout(time_out)
        .pool_timeout(time_out)
        .get_updates_read_timeout(time_out)
        .get_updates_write_timeout(time_out)
        .get_updates_pool_timeout(time_out)
        .get_updates_connect_timeout(time_out)
        .rate_limiter(AIORateLimiter(max_retries=5))
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("info", info))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("reset", reset_chat))
    application.add_handler(CommandHandler("en2zh", lambda update, context: command_bot(update, context, "Simplified Chinese")))
    application.add_handler(CommandHandler("zh2en", lambda update, context: command_bot(update, context, "english")))
    application.add_handler(CommandHandler("updateprompt", update_system_prompt))
    application.add_handler(InlineQueryHandler(inlinequery))
    application.add_handler(CallbackQueryHandler(button_press))
    application.add_handler(MessageHandler((filters.TEXT | filters.VOICE) & ~filters.COMMAND, lambda update, context: command_bot(update, context, prompt=None, has_command=False), block = False))
    application.add_handler(MessageHandler(filters.CAPTION & ((filters.PHOTO & ~filters.COMMAND) | (filters.Document.FileExtension("jpg") | filters.Document.FileExtension("jpeg") | filters.Document.FileExtension("png"))), lambda update, context: command_bot(update, context, prompt=None, has_command=False)))
    application.add_handler(MessageHandler(~filters.CAPTION & ((filters.PHOTO & ~filters.COMMAND) | (filters.Document.PDF | filters.Document.TXT | filters.Document.DOC | filters.Document.FileExtension("jpg") | filters.Document.FileExtension("jpeg") | filters.Document.FileExtension("md") | filters.Document.FileExtension("py") | filters.AUDIO | filters.Document.FileExtension("wav"))), handle_file))
    application.add_handler(MessageHandler(filters.COMMAND, unknown))
    application.add_error_handler(error)

    if WEB_HOOK:
        print("WEB_HOOK:", WEB_HOOK)
        application.run_webhook("0.0.0.0", PORT, webhook_url=WEB_HOOK)
    else:
        application.run_polling(timeout=time_out)
