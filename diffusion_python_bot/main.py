import sys
import datetime
import discord
from discord import Thread
import asyncio
from asyncio import ( Lock, Queue, Semaphore )
import traceback
from discord.ext import commands
from PIL import Image
from io import BytesIO
from classes.app_config import AppConfig
from classes.image_generator import ImageGenerator
from classes.message_handler import MessageHandler
from user_commands import UserCommands
from utils import _get_project_meta
from classes.discord_progress_bar import DiscordProgressBar
from classes.tqdm_capture import TqdmCapture
import logging

pkg_meta = _get_project_meta()
pkg_name = str(pkg_meta["name"])
# The short X.Y version
pkg_version = str(pkg_meta["version"])

config = AppConfig()
# How many concurrent slots to run when generating images.
concurrent_slots = config.get_concurrent_slots()
TOKEN = config.get_discord_api_key()

intents = discord.Intents.default()
intents.typing = False
intents.presences = False
intents.message_content = True

from classes.discord_wrapper import DiscordWrapper
bot = DiscordWrapper(command_prefix="!", intents=intents)

# Configure the root logger to equate Discord's logging settings.
discord_logger = logging.getLogger('discord')
# Add a file handler to log to a file
file_handler = logging.FileHandler(filename='main.log', encoding='utf-8', mode='w')
file_handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s'))
discord_logger.addHandler(file_handler)
logging.getLogger().setLevel(logging.DEBUG)
logging.getLogger().handlers = discord_logger.handlers

logging.basicConfig(level=logging.INFO)

# Threaded conversation handling
image_queue = Queue()
appconfig_lock = Lock()
image_queue_lock = Lock()
image_generation_semaphore = Semaphore(concurrent_slots)

asyncio.run(bot.add_cog(UserCommands(bot, appconfig_lock, config)))
image_generator = ImageGenerator(image_queue_lock)
message_handler = MessageHandler(
    image_generator=image_generator,
    config=config,
    shared_queue=image_queue,
    shared_queue_lock=image_queue_lock,
)
message_handler.set_bot(bot)


@bot.event
async def on_ready():
    logging.info(f"{bot.user} has connected to Discord!")
    logging.info(f"Server(s) connected: {[guild.name for guild in bot.guilds]}")


async def send_large_message(ctx, text, max_chars=2000):
    if len(text) <= max_chars:
        await ctx.send(text)
        return

    lines = text.split("\n")
    buffer = ""
    first_message = None
    for line in lines:
        if len(buffer) + len(line) + 1 > max_chars:
            if not first_message:
                first_message = await ctx.send(buffer)
                thread = await first_message.channel.create_thread(name="Model List")
            else:
                await thread.send_message(buffer)
            buffer = ""
        buffer += line + "\n"

    if buffer:
        await thread.send_message(buffer)


@bot.command(name="ping", help="Test command to check if the bot is responding.")
async def ping(ctx):
    await ctx.send("Pong!")


async def generate_image_from_queue():
    while not image_queue.empty():
        ctx, prompt, discord_first_message = await image_queue.get()
        logging.info("Create progress bar using {discord_first_message}...")
        await discord_first_message.edit(content="Begin processing queue item: " + prompt)
        progress_bar = DiscordProgressBar(ctx=ctx, total_steps=100, original_stdout=sys.stdout, progress_message=discord_first_message)
        tqdm_file = TqdmCapture(progress_bar, bot.loop, sys.stdout, sys.stderr)
        logging.info("Editing initial message.")
        await discord_first_message.edit(content="Begin image generation: " + prompt)
        user_id = ctx.author.id
        await ctx.message.delete()
        async with appconfig_lock:
            user_config = config.get_user_config(user_id)
            steps = config.get_user_setting(user_id, "steps", 50)
            negative_prompt = config.get_user_setting(
                user_id,
                "negative_prompt",
                "(child, baby, deformed, distorted, disfigured:1.3), poorly drawn, bad anatomy, wrong anatomy, extra limb, missing limb, floating limbs, (mutated hands and fingers:1.4), disconnected limbs, mutation, mutated, ugly, disgusting, blurry, amputation",
            )
            positive_prompt = config.get_user_setting(
                user_id, "positive_prompt", "beautiful hyperrealistic"
            )
            resolution = config.get_user_setting(
                user_id, "resolution", {"width": 800, "height": 456}
            )
            model_id = user_config.get("model", None)
        try:
            # Run the image generation in an executor
            # Acquire the semaphore
            await image_generation_semaphore.acquire()
            async with image_queue_lock:
                logging.info("Begin capture...")
                image = await bot.loop.run_in_executor(
                    None,
                    image_generator.generate_image,
                    prompt,
                    model_id,
                    resolution,
                    negative_prompt,
                    steps,
                    positive_prompt,
                    tqdm_file,
                )
            buffer = BytesIO()
            image.save(buffer, "PNG")
            buffer.seek(0)
            message = (
                "**Requested by:** "
                + str(ctx.author.mention)
                + "\n**Prompt:** "
                + str(prompt)
                + "\n**Model:** "
                + str(model_id)
                + "\n**Resolution:** "
                + str(resolution["width"])
                + "x"
                + str(resolution["height"])
                + "\n**Steps:** "
                + str(steps)
            )
            if isinstance(ctx.message.channel, Thread):
                thread = ctx.message.channel
            else:
                await discord_first_message.edit(content="Prompt by **" + ctx.author.name + "**: `" + prompt + "`")
                thread = await discord_first_message.create_thread(name=prompt[:97] + '...', auto_archive_duration=60) # You can change the duration (in minutes) as needed
            await thread.send(
                content=message, file=discord.File(buffer, "generated_image.png")
            )
            await discord_first_message.delete()
        except discord.NotFound:
            logging.info("Discord message was already deleted, probably.")
        except Exception as e:
            error_message = (
                f"Error generating image: {e}\n\nStack trace:\n{traceback.format_exc()}"
            )
            await send_large_message(ctx, f"Error generating image: {error_message}")
        finally:
            image_queue.task_done()
            image_generation_semaphore.release()

@bot.command(name="generate", help="Generates an image based on the given prompt.")
async def generate(ctx, *, prompt):
    try:
        print("Begin generate command coroutine.")
        discord_first_message = await ctx.send(f"Adding prompt to queue for processing: " + prompt)
        # Put the context and prompt in a tuple before adding it to the queue
        await image_queue.put((ctx, prompt, discord_first_message))

        # Get the number of concurrent slots
        concurrent_slots = config.get_concurrent_slots()

        # Check if there are any running tasks
        if not hasattr(bot, "image_generation_tasks"):
            bot.image_generation_tasks = []

        # Remove any completed tasks
        bot.image_generation_tasks = [t for t in bot.image_generation_tasks if not t.done()]

        # If there are fewer tasks than allowed slots, create new tasks
        while len(bot.image_generation_tasks) < concurrent_slots:
            task = bot.loop.create_task(generate_image_from_queue())
            bot.image_generation_tasks.append(task)

    except Exception as e:
        await ctx.send(
            f"Error generating image: {e}\n\nStack trace:\n{traceback.format_exc()}"
        )

# Set model command with AppConfig lock
@bot.command(name="setmodel", help="Set the default model for the user.")
async def set_model(ctx, *, model_id=None):
    user_id = ctx.author.id
    async with appconfig_lock:
        user_config = config.get_user_config(user_id)
        if model_id is None:
            model_id = user_config.get("model", None)
            if model_id is None:
                model_id = config.get_default_model()
            await ctx.send(f"Your current model is set to {model_id}.")
            return
        user_config["model"] = model_id
        config.set_user_config(user_id, user_config)
    await ctx.send(
        f"Default model for user {ctx.author.name} has been set to {model_id}."
    )


@bot.command(name="steps", help="Sets the intensity for generated images. Max 1500.")
async def set_steps(ctx, steps: int = None):
    user_id = ctx.author.id
    async with appconfig_lock:
        user_config = config.get_user_config(user_id)
        if steps is None:
            steps = config.get_user_setting(user_id, "steps", 50)
            await ctx.send("Your current steps are set to " + str(steps))
        else:
            user_config["steps"] = steps
            config.set_user_config(user_id, user_config)
            await ctx.send("Your steps have been updated to " + str(steps))


# Set resolution command with AppConfig lock
available_resolutions = asyncio.run(image_generator.list_available_resolutions())


@bot.command(
    name="resolution",
    help="Set or get your default resolution for generated images.\nAvailable resolutions:\n"
    + str(available_resolutions),
)
async def set_resolution(ctx, resolution=None):
    user_id = ctx.author.id
    async with appconfig_lock:
        user_config = config.get_user_config(user_id)
        available_resolutions = await image_generator.list_available_resolutions()
        if resolution is None:
            resolution = user_config.get("resolution", {"width": 800, "height": 456})
            await ctx.send(
                f'Your current resolution is set to {resolution["width"]}x{resolution["height"]}.\nAvailable resolutions:\n'
                + available_resolutions
            )
            return

        if "x" in resolution:
            width, height = map(int, resolution.split("x"))
        else:
            width, height = map(int, resolution.split())

        if not image_generator.is_valid_resolution(width, height):
            await ctx.send(
                f"Invalid resolution. Available resolutions:\n" + available_resolutions
            )
            return

        user_config["resolution"] = {"width": width, "height": height}
        config.set_user_config(user_id, user_config)
        await ctx.send(
            f"Default resolution set to {width}x{height} for user {ctx.author.name}."
        )


@bot.command(name="listmodels", help="Lists the available models from Hugging Face.")
async def list_models(ctx):
    try:
        models = image_generator.get_available_models()
        models_text = "Available models:\n" + "\n".join(models)
        await send_large_message(ctx, models_text)
    except Exception as e:
        await ctx.send(
            f"Error retrieving models: {e}\n\nStack trace:\n{traceback.format_exc()}"
        )


# Negative prompt command with AppConfig lock
@bot.command(name="negative", help="Gets or sets the negative prompt.")
async def negative(ctx, *, negative_prompt=None):
    user_id = ctx.author.id
    async with appconfig_lock:
        user_config = config.get_user_config(user_id)
        if negative_prompt is None:
            negative_prompt = user_config.get(
                "negative_prompt",
                "(deformed, distorted, disfigured:1.3), poorly drawn, bad anatomy, wrong anatomy, extra limb, missing limb, floating limbs, (mutated hands and fingers:1.4), disconnected limbs, mutation, mutated, ugly, disgusting, blurry, amputation",
            )
            output_text = (
                "Your negative prompt currently is set as:\n" + negative_prompt
            )
        else:
            user_config["negative_prompt"] = negative_prompt
            config.set_user_config(user_id, user_config)
            output_text = "Your negative prompt is now set to:\n" + negative_prompt
    await send_large_message(ctx, output_text)


# Positive prompt command with AppConfig lock
@bot.command(
    name="positive",
    help='Gets or sets the positive POST-prompt. A value of "none" disables this. It is added to the end of every prompt you submit via !generate.',
)
async def positive(ctx, *, positive_prompt=None):
    user_id = ctx.author.id
    async with appconfig_lock:
        user_config = config.get_user_config(user_id)
        if positive_prompt is None:
            positive_prompt = user_config.get(
                "positive_prompt", "beautiful hyperrealistic"
            )
            output_text = (
                "Your positive prompt currently is set as:\n" + positive_prompt
            )
        elif positive_prompt.lower() == "none":
            user_config["positive_prompt"] = ""
            config.set_user_config(user_id, user_config)
            output_text = "Your positive prompt is now set to:\n" + positive_prompt
        else:
            user_config["positive_prompt"] = positive_prompt
            config.set_user_config(user_id, user_config)
            output_text = "Your positive prompt is now set to:\n" + positive_prompt
    await send_large_message(ctx, output_text)


## Image queue management
def is_server_admin(ctx):
    admin_role_name = (
        "Image Admin"  # Replace this with the role name used on your server for admins
    )
    return any(role.name == admin_role_name for role in ctx.author.roles)


@bot.command(
    name="listqueue",
    help="Lists the contents of the image generation queue (Admin only).",
)
async def list_queue(ctx):
    if not is_server_admin(ctx):
        await ctx.send("You must be a server admin to use this command.")
        return

    if image_queue.empty():
        await ctx.send("The image generation queue is currently empty.")
        return

    queue_contents = [
        f"{index}: {item[1]}" for index, item in enumerate(image_queue._queue)
    ]
    await send_large_message(
        ctx, "Image generation queue:\n" + "\n".join(queue_contents)
    )


@bot.command(
    name="removequeue",
    help="Removes an entry from the image generation queue by index (Admin only).",
)
async def remove_queue(ctx, index: int):
    if not is_server_admin(ctx):
        await ctx.send("You must be a server admin to use this command.")
        return
    global image_queue

    new_queue, removed_item = await remove_item_from_queue(image_queue, index)
    if removed_item is None:
        await ctx.send("Invalid queue index. Please provide a valid index.")
        return

    image_queue = new_queue
    await ctx.send(f"Removed item '{removed_item[1]}' from the image generation queue.")


async def remove_item_from_queue(queue, index):
    if index < 0 or index >= queue.qsize():
        return None
    new_queue = asyncio.Queue()
    removed_item = None
    for i in range(queue.qsize()):
        item = await queue.get()
        if i == index:
            removed_item = item
        else:
            await new_queue.put(item)

    return new_queue, removed_item


async def generate_variants(image_generator, input_image):
    prompt = "a variation of the input image"
    generated_image = image_generator.generate_image(
        prompt, model_id="lambdalabs/sd-image-variations-diffusers", steps=25
    )
    return [generated_image]


@bot.event
async def on_message(message):
    # If the message is from the bot itself, ignore it
    if message.author == bot.user:
        return
    print("Actually we did hit it: " + str(message))
    await message_handler.handle_message(message)


def main():
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logging.info(f"[{timestamp}] bot version {pkg_version}...")
    logging.info(f"[{timestamp}] Starting bot...")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
