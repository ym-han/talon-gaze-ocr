import glob
import logging
import sys
from math import floor
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence

import numpy as np
from talon import Context, Module, actions, app, cron, fs, screen, settings
from talon.canvas import Canvas
from talon.skia.typeface import Fontstyle, Typeface
from talon.types import rect

from .timestamped_captures import TextRange, TimestampedText

try:
    from talon.experimental import ocr
except ImportError:
    ocr = None

# Adjust path to search adjacent package directories. Prefixed with dot to avoid
# Talon running them itself. Append to search path so that faster binary
# packages can be used instead if available.
subtree_dir = Path(__file__).parent / ".subtrees"
package_paths = [
    str(subtree_dir / "gaze-ocr"),
    str(subtree_dir / "screen-ocr"),
    str(subtree_dir / "rapidfuzz/src"),
    str(subtree_dir / "jarowinkler/src"),
]
saved_path = sys.path.copy()
try:
    sys.path.extend([path for path in package_paths if path not in sys.path])
    import gaze_ocr
    import gaze_ocr.talon
    import screen_ocr  # dependency of gaze-ocr
finally:
    # Restore the unmodified path.
    sys.path = saved_path.copy()

mod = Module()
ctx = Context()

mod.setting(
    "ocr_use_talon_backend",
    type=bool,
    default=True,
    desc="If true, use Talon backend, otherwise use default fast backend from screen_ocr.",
)
mod.setting(
    "ocr_connect_tracker",
    type=bool,
    default=True,
    desc="If true, automatically connect the eye tracker at startup.",
)
mod.setting(
    "ocr_logging_dir",
    type=str,
    default=None,
    desc="If specified, log OCR'ed images to this directory.",
)
mod.setting(
    "ocr_click_offset_right",
    type=int,
    default=0,
    desc="Adjust the X-coordinate when clicking around OCR text.",
)
mod.setting(
    "ocr_select_pause_seconds",
    type=float,
    default=0.5,
    desc="Adjust the pause between clicks when performing a selection.",
)
mod.setting(
    "ocr_debug_display_seconds",
    type=float,
    default=2,
    desc="Adjust how long debugging display is shown.",
)
mod.setting(
    "ocr_disambiguation_display_seconds",
    type=float,
    default=5,
    desc="Adjust how long disambiguation display is shown. Use 0 to remove timeout.",
)
mod.setting(
    "ocr_gaze_box_padding",
    type=int,
    default=100,
    desc="How much padding is applied to gaze bounding box when searching for text.",
)
mod.setting(
    "ocr_gaze_point_padding",
    type=int,
    default=200,
    desc="How much padding is applied to gaze point when searching for text.",
)

mod.mode("gaze_ocr_disambiguation")
mod.list("ocr_actions", desc="Actions to perform on selected text.")
mod.list("ocr_modifiers", desc="Modifiers to perform on selected text.")
ctx.lists["self.ocr_actions"] = {
    "take": "select",
    "copy": "copy",
    "carve": "cut",
    "paste to": "paste",
    "paste link to": "paste_link",
    "clear": "delete",
    "change": "delete",
    "delete": "delete_with_whitespace",
    "chuck": "delete_with_whitespace",
    "cap": "capitalize",
    "no cap": "uncapitalize",
    "no caps": "uncapitalize",
    "lower": "lowercase",
    "upper": "uppercase",
    # Note: the following are not defined by default in knausj.
    "bold": "bold",
    "italic": "italic",
    "strikethrough": "strikethrough",
    "number": "number_list",
    "bullet": "bullet_list",
    "link": "link",
}
ctx.lists["self.ocr_modifiers"] = {
    "all": "selectAll",
}


def add_homophones(
    homophones: Dict[str, Sequence[str]], to_add: Iterable[Iterable[str]]
):
    for words in to_add:
        merged_words = set(words)
        for word in words:
            old_words = homophones.get(word.lower(), [])
            merged_words.update(old_words)
        merged_words = sorted(merged_words)
        for word in merged_words:
            homophones[word.lower()] = merged_words


digits = "zero one two three four five six seven eight nine".split()
default_digits_map = {n: i for i, n in enumerate(digits)}

# Inline punctuation words in case people are using vanilla knausj, where these are not exposed.
default_punctuation_words = {
    "back tick": "`",
    "grave": "`",
    "comma": ",",
    "period": ".",
    "full stop": ".",
    "semicolon": ";",
    "colon": ":",
    "forward slash": "/",
    "question mark": "?",
    "exclamation mark": "!",
    "exclamation point": "!",
    "asterisk": "*",
    "hash sign": "#",
    "number sign": "#",
    "percent sign": "%",
    "at sign": "@",
    "and sign": "&",
    "ampersand": "&",
    # Currencies
    "dollar sign": "$",
    "pound sign": "£",
    "hyphen": "-",
    "underscore": "_",
}


user_dir = Path(__file__).parents[1]
# Search user_dir to find homophones.csv
homophones_file = None
for path in glob.glob(str(user_dir / "**/homophones.csv"), recursive=True):
    homophones_file = path
    break
if homophones_file:
    logging.info(f"Found homophones file: {homophones_file}")
else:
    logging.warning(f"Could not find homophones.csv. Is knausj_talon installed?")


def get_knausj_homophones():
    phones = {}
    if not homophones_file:
        return phones
    with open(homophones_file) as f:
        for line in f:
            words = line.rstrip().split(",")
            merged_words = set(words)
            for word in words:
                old_words = phones.get(word.lower(), [])
                merged_words.update(old_words)
            merged_words = sorted(merged_words)
            for word in merged_words:
                phones[word.lower()] = merged_words
    return phones


def reload_backend(name, flags):
    # Initialize eye tracking and OCR.
    global tracker, ocr_reader, gaze_ocr_controller
    tracker = gaze_ocr.talon.TalonEyeTracker()
    # Note: tracker is connected automatically in the constructor.
    if not settings.get("user.ocr_connect_tracker"):
        tracker.disconnect()
    homophones = get_knausj_homophones()
    # TODO: Get this through an action to support customization.
    add_homophones(
        homophones, [(str(num), spoken) for spoken, num in default_digits_map.items()]
    )
    # Attempt to use overridable action to get punctuation. This is available in
    # wolfmanstout_talon, but not yet in knausj_talon, so fallback if needed.
    try:
        punctuation_words = actions.user.get_punctuation_words()
    except KeyError:
        punctuation_words = default_punctuation_words
    add_homophones(
        homophones,
        [
            (punctuation, spoken)
            for spoken, punctuation in punctuation_words.items()
            if " " not in spoken
        ],
    )
    add_homophones(
        homophones,
        [
            # 0k is not actually a homophone but is frequently produced by OCR.
            ("ok", "okay", "0k"),
        ],
    )
    setting_ocr_use_talon_backend = settings.get("user.ocr_use_talon_backend")
    if setting_ocr_use_talon_backend and ocr:
        ocr_reader = screen_ocr.Reader.create_reader(
            backend="talon",
            radius=settings.get("user.ocr_gaze_point_padding"),
            homophones=homophones,
        )
    else:
        if setting_ocr_use_talon_backend and not ocr:
            logging.info("Talon OCR not available, will rely on external support.")
        ocr_reader = screen_ocr.Reader.create_fast_reader(
            radius=settings.get("user.ocr_gaze_point_padding"), homophones=homophones
        )
    gaze_ocr_controller = gaze_ocr.Controller(
        ocr_reader,
        tracker,
        mouse=gaze_ocr.talon.Mouse(),
        keyboard=gaze_ocr.talon.Keyboard(),
        app_actions=gaze_ocr.talon.AppActions(),
        save_data_directory=settings.get("user.ocr_logging_dir"),
        gaze_box_padding=settings.get("user.ocr_gaze_box_padding"),
    )


def on_ready():
    reload_backend(None, None)
    if homophones_file:
        fs.watch(str(homophones_file), reload_backend)


app.register("ready", on_ready)


def has_light_background(screenshot):
    array = np.array(screenshot)
    # From https://pillow.readthedocs.io/en/stable/reference/Image.html#PIL.Image.Image.convert
    grayscale = 0.299 * array[:, :, 0] + 0.587 * array[:, :, 1] + 0.114 * array[:, :, 2]
    return np.mean(grayscale) > 128


disambiguation_canvas = None
debug_canvas = None
ambiguous_matches: Optional[Sequence[gaze_ocr.CursorLocation]] = None
disambiguation_generator = None


def reset_disambiguation():
    global ambiguous_matches, disambiguation_generator, disambiguation_canvas, debug_canvas
    ambiguous_matches = None
    disambiguation_generator = None
    hide_canvas = disambiguation_canvas or debug_canvas
    if disambiguation_canvas:
        disambiguation_canvas.close()
    disambiguation_canvas = None
    if debug_canvas:
        debug_canvas.close()
    debug_canvas = None
    if hide_canvas:
        # Ensure that the canvas doesn't interfere with subsequent screenshots.
        actions.sleep("10ms")


def show_disambiguation():
    global ambiguous_matches, disambiguation_canvas

    def on_draw(c):
        assert ambiguous_matches
        contents = gaze_ocr_controller.latest_screen_contents()
        debug_color = (
            "000000" if has_light_background(contents.screenshot) else "ffffff"
        )
        nearest = gaze_ocr_controller.find_nearest_cursor_location(ambiguous_matches)
        used_locations = set()
        for i, match in enumerate(ambiguous_matches):
            if nearest == match:
                c.paint.typeface = Typeface.from_name(
                    "", Fontstyle.new(weight=700, width=5)
                )
            else:
                c.paint.typeface = ""
            c.paint.textsize = max(round(match.text_height * 2), 15)
            c.paint.style = c.paint.Style.FILL
            c.paint.color = debug_color
            location = (match.visual_coordinates[0], match.visual_coordinates[1])
            # TODO: Check for nearby used locations, not just identical.
            while location in used_locations:
                # Shift right.
                location = (location[0] + match.text_height, location[1])
            used_locations.add(location)
            c.draw_text(str(i + 1), *location)
        setting_ocr_disambiguation_display_seconds = settings.get(
            "user.ocr_disambiguation_display_seconds"
        )
        if setting_ocr_disambiguation_display_seconds:
            cron.after(
                f"{setting_ocr_disambiguation_display_seconds}s",
                disambiguation_canvas.close,
            )

    actions.mode.enable("user.gaze_ocr_disambiguation")
    if disambiguation_canvas:
        disambiguation_canvas.close()
    disambiguation_canvas = Canvas.from_screen(screen.main())
    disambiguation_canvas.register("draw", on_draw)
    disambiguation_canvas.freeze()


def begin_generator(generator):
    global ambiguous_matches, disambiguation_generator, disambiguation_canvas
    reset_disambiguation()
    try:
        ambiguous_matches = next(generator)
        disambiguation_generator = generator
        show_disambiguation()
    except StopIteration:
        # Execution completed without need for disambiguation.
        pass


def move_cursor_to_word_generator(text: TimestampedText):
    result = yield from gaze_ocr_controller.move_cursor_to_words_generator(
        text.text,
        disambiguate=True,
        time_range=(text.start, text.end),
        click_offset_right=settings.get("user.ocr_click_offset_right"),
    )
    if not result:
        actions.user.show_ocr_overlay_for_query("text", f"{text.text}")
        raise RuntimeError('Unable to find: "{}"'.format(text))


def move_text_cursor_to_word_generator(
    text: TimestampedText,
    position: str,
    hold_shift: bool = False,
):
    result = yield from gaze_ocr_controller.move_text_cursor_to_words_generator(
        text.text,
        disambiguate=True,
        cursor_position=position,
        time_range=(text.start, text.end),
        click_offset_right=settings.get("user.ocr_click_offset_right"),
        hold_shift=hold_shift,
    )
    if not result:
        actions.user.show_ocr_overlay_for_query("text", f"{text.text}")
        raise RuntimeError('Unable to find: "{}"'.format(text))


def move_text_cursor_to_longest_prefix_generator(
    text: TimestampedText, position: str, hold_shift: bool = False
):
    (
        locations,
        prefix_length,
    ) = yield from gaze_ocr_controller.move_text_cursor_to_longest_prefix_generator(
        text.text,
        disambiguate=True,
        cursor_position=position,
        time_range=(text.start, text.end),
        click_offset_right=settings.get("user.ocr_click_offset_right"),
        hold_shift=hold_shift,
    )
    if not locations:
        actions.user.show_ocr_overlay_for_query("text", f"{text.text}")
        raise RuntimeError('Unable to find: "{}"'.format(text))
    return prefix_length


def move_text_cursor_to_longest_suffix_generator(
    text: TimestampedText, position: str, hold_shift: bool = False
):
    (
        locations,
        prefix_length,
    ) = yield from gaze_ocr_controller.move_text_cursor_to_longest_suffix_generator(
        text.text,
        disambiguate=True,
        cursor_position=position,
        time_range=(text.start, text.end),
        click_offset_right=settings.get("user.ocr_click_offset_right"),
        hold_shift=hold_shift,
    )
    if not locations:
        actions.user.show_ocr_overlay_for_query("text", f"{text.text}")
        raise RuntimeError('Unable to find: "{}"'.format(text))
    return prefix_length


def move_text_cursor_to_difference(text: TimestampedText):
    result = yield from gaze_ocr_controller.move_text_cursor_to_difference_generator(
        text.text,
        disambiguate=True,
        time_range=(text.start, text.end),
        click_offset_right=settings.get("user.ocr_click_offset_right"),
    )
    if not result:
        actions.user.show_ocr_overlay_for_query("text", f"{text.text}")
        raise RuntimeError('Unable to find: "{}"'.format(text))
    return result


def select_text_generator(
    start: TimestampedText,
    end: Optional[TimestampedText] = None,
    for_deletion: bool = False,
    after_start: bool = False,
    before_end: bool = False,
):
    start_text = start.text
    end_text = end.text if end else None
    result = yield from gaze_ocr_controller.select_text_generator(
        start_text,
        disambiguate=True,
        end_words=end_text,
        for_deletion=for_deletion,
        start_time_range=(start.start, start.end),
        end_time_range=(end.start, end.end) if end else None,
        click_offset_right=settings.get("user.ocr_click_offset_right"),
        after_start=after_start,
        before_end=before_end,
        select_pause_seconds=settings.get("user.ocr_select_pause_seconds"),
    )
    if not result:
        actions.user.show_ocr_overlay_for_query(
            "text", f"{start.text}...{end.text if end else None}"
        )
        raise RuntimeError('Unable to select "{}" to "{}"'.format(start, end))


def select_matching_text_generator(text: TimestampedText):
    result = yield from gaze_ocr_controller.select_matching_text_generator(
        text.text,
        disambiguate=True,
        time_range=(text.start, text.end),
        click_offset_right=settings.get("user.ocr_click_offset_right"),
        select_pause_seconds=settings.get("user.ocr_select_pause_seconds"),
    )
    if not result:
        actions.user.show_ocr_overlay_for_query("text", f"{text.text}")
        raise RuntimeError('Unable to find: "{}"'.format(text))


def perform_ocr_action_generator(
    ocr_action: str,
    ocr_modifier: str,
    text_range: TextRange,
    for_deletion: Optional[bool] = None,
):
    if not text_range.start:
        assert text_range.end
        yield from move_text_cursor_to_word_generator(
            text_range.end,
            position="before" if text_range.before_end else "after",
            hold_shift=True,
        )
    else:
        for_deletion = (
            for_deletion
            if for_deletion is not None
            else ocr_action in ("cut", "delete_with_whitespace")
        )
        yield from select_text_generator(
            text_range.start,
            text_range.end,
            for_deletion,
            after_start=text_range.after_start,
            before_end=text_range.before_end,
        )
    if ocr_modifier == "":
        pass
    elif ocr_modifier == "selectAll":
        actions.edit.select_all()
    else:
        raise RuntimeError(f"Modifier not supported: {ocr_modifier}")

    if ocr_action == "select":
        pass
    elif ocr_action == "copy":
        actions.edit.copy()
    elif ocr_action == "cut":
        actions.edit.cut()
    elif ocr_action == "paste":
        actions.edit.paste()
    elif ocr_action == "paste_link":
        actions.user.hyperlink()
        actions.sleep("100ms")
        actions.edit.paste()
    elif ocr_action in ("delete", "delete_with_whitespace"):
        actions.key("backspace")
    elif ocr_action == "capitalize":
        text = actions.edit.selected_text()
        actions.insert(text[0].capitalize() + text[1:] if text else "")
    elif ocr_action == "uncapitalize":
        text = actions.edit.selected_text()
        actions.insert(text[0].lower() + text[1:] if text else "")
    elif ocr_action == "lowercase":
        text = actions.edit.selected_text()
        actions.insert(text.lower())
    elif ocr_action == "uppercase":
        text = actions.edit.selected_text()
        actions.insert(text.upper())
    elif ocr_action == "bold":
        actions.user.bold()
    elif ocr_action == "italic":
        actions.user.italic()
    elif ocr_action == "strikethrough":
        actions.user.strikethrough()
    elif ocr_action == "number_list":
        actions.user.number_list()
    elif ocr_action == "bullet_list":
        actions.user.bullet_list()
    elif ocr_action == "link":
        actions.user.hyperlink()
    else:
        raise RuntimeError(f"Action not supported: {ocr_action}")


def context_sensitive_insert(text: str):
    if settings.get("user.context_sensitive_dictation"):
        actions.user.dictation_insert(text)
    else:
        # Use the default insert because the dictation context is likely wrong.
        actions.insert(text)


@mod.action_class
class GazeOcrActions:
    def connect_ocr_eye_tracker():
        """Connects eye tracker to OCR."""
        tracker.connect()

    def disconnect_ocr_eye_tracker():
        """Disconnects eye tracker from OCR."""
        tracker.disconnect()

    def move_cursor_to_word(text: TimestampedText):
        """Moves cursor to onscreen word."""
        begin_generator(move_cursor_to_word_generator(text))

    def move_text_cursor_to_word(
        text: TimestampedText,
        position: str,
    ):
        """Moves text cursor near onscreen word."""
        begin_generator(move_text_cursor_to_word_generator(text, position))

    def move_cursor_to_gaze_point(offset_right: int = 0, offset_down: int = 0):
        """Moves mouse cursor to gaze location."""
        tracker.move_to_gaze_point((offset_right, offset_down))

    def perform_ocr_action(
        ocr_action: str,
        ocr_modifier: str,
        text_range: TextRange,
    ):
        """Selects text and performs an action."""
        begin_generator(
            perform_ocr_action_generator(ocr_action, ocr_modifier, text_range)
        )

    def replace_text(ocr_modifier: str, text_range: TextRange, replacement: str):
        """Replaces onscreen text."""

        def run():
            yield from perform_ocr_action_generator(
                "select",
                ocr_modifier,
                text_range,
                for_deletion=settings.get("user.context_sensitive_dictation"),
            )
            context_sensitive_insert(replacement)

        begin_generator(run())

    def insert_adjacent_to_text(
        find_text: TimestampedText, position: str, insertion_text: str
    ):
        """Insert text adjacent to onscreen text."""

        def run():
            yield from move_text_cursor_to_word_generator(
                find_text,
                position,
            )
            context_sensitive_insert(insertion_text)

        begin_generator(run())

    def append_text(text: TimestampedText):
        """Finds onscreen text that matches the beginning of the provided text and
        appends the rest to it."""

        def run():
            prefix_length = yield from move_text_cursor_to_longest_prefix_generator(
                text, "after"
            )
            insertion_text = text.text[prefix_length:]
            context_sensitive_insert(insertion_text)

        begin_generator(run())

    def prepend_text(text: TimestampedText):
        """Finds onscreen text that matches the end of the provided text and
        prepends the rest to it."""

        def run():
            suffix_length = yield from move_text_cursor_to_longest_suffix_generator(
                text, "before"
            )
            insertion_text = text.text[:-suffix_length]
            context_sensitive_insert(insertion_text)

        begin_generator(run())

    def insert_text_difference(text: TimestampedText):
        """Finds onscreen text that matches the start and/or end of the provided text
        and inserts the difference."""

        def run():
            start, end = yield from move_text_cursor_to_difference(text)
            insertion_text = text.text[start:end]
            context_sensitive_insert(insertion_text)

        begin_generator(run())

    def revise_text(text: TimestampedText):
        """Finds onscreen text that matches the beginning and end of the provided text
        and replaces it."""

        def run():
            yield from select_matching_text_generator(text)
            insertion_text = text.text
            context_sensitive_insert(insertion_text)

        begin_generator(run())

    def revise_text_starting_with(text: TimestampedText):
        """Finds onscreen text that matches the beginning of the provided text
        and replaces it until the caret."""

        def run():
            try:
                yield from move_text_cursor_to_longest_prefix_generator(
                    text, "before", hold_shift=True
                )
            except RuntimeError as e:
                # Keep going so the user doesn't lose the dictated text.
                print(e)
            insertion_text = text.text
            context_sensitive_insert(insertion_text)

        begin_generator(run())

    def revise_text_ending_with(text: TimestampedText):
        """Finds onscreen text that matches the end of the provided text and
        replaces it from the caret."""

        def run():
            try:
                yield from move_text_cursor_to_longest_suffix_generator(
                    text, "after", hold_shift=True
                )
            except RuntimeError as e:
                # Keep going so the user doesn't lose the dictated text.
                print(e)
            insertion_text = text.text
            context_sensitive_insert(insertion_text)

        begin_generator(run())

    def show_ocr_overlay(type: str, near: Optional[TimestampedText] = None):
        """Displays overlay over primary screen.

        Reads nearby gaze when the near parameter is spoken."""
        reset_disambiguation()
        if near:
            gaze_ocr_controller.read_nearby((near.start, near.end))
        else:
            gaze_ocr_controller.read_nearby()
        actions.user.show_ocr_overlay_for_query(type)

    def show_ocr_overlay_for_query(type: str, query: str = ""):
        """Display overlay over primary screen, displaying the query."""
        global debug_canvas
        if debug_canvas:
            debug_canvas.close()
        contents = gaze_ocr_controller.latest_screen_contents()

        def on_draw(c):
            debug_color = (
                "000000" if has_light_background(contents.screenshot) else "ffffff"
            )
            # Show bounding box.
            c.paint.style = c.paint.Style.STROKE
            c.paint.color = debug_color
            c.draw_rect(
                rect.Rect(
                    x=contents.bounding_box[0],
                    y=contents.bounding_box[1],
                    width=contents.bounding_box[2] - contents.bounding_box[0],
                    height=contents.bounding_box[3] - contents.bounding_box[1],
                )
            )
            if contents.screen_coordinates:
                c.paint.style = c.paint.Style.STROKE
                c.paint.color = debug_color
                c.draw_circle(
                    contents.screen_coordinates[0],
                    contents.screen_coordinates[1],
                    contents.search_radius,
                )
            if query:
                c.paint.typeface = ""
                c.paint.textsize = 30
                c.paint.style = c.paint.Style.FILL
                c.paint.color = "FFFFFF"
                c.draw_text(query, x=screen.main().x + screen.main().width / 2, y=20)
                c.paint.style = c.paint.Style.STROKE
                c.paint.color = "000000"
                c.draw_text(query, x=screen.main().x + screen.main().width / 2, y=20)
            for line in contents.result.lines:
                for word in line.words:
                    if type == "text":
                        c.paint.typeface = ""
                        c.paint.textsize = floor(word.height)
                        c.paint.style = c.paint.Style.FILL
                        c.paint.color = debug_color
                        c.draw_text(word.text, word.left, word.top)
                    elif type == "boxes":
                        c.paint.style = c.paint.Style.STROKE
                        c.paint.color = debug_color
                        c.draw_rect(
                            rect.Rect(
                                x=word.left,
                                y=word.top,
                                width=word.width,
                                height=word.height,
                            )
                        )
                    else:
                        raise RuntimeError(f"Type not recognized: {type}")
            cron.after(
                f"{settings.get('user.ocr_debug_display_seconds')}s", debug_canvas.close
            )

        debug_canvas = Canvas.from_screen(screen.main())
        debug_canvas.register("draw", on_draw)
        debug_canvas.freeze()

    def choose_gaze_ocr_option(index: int):
        """Disambiguate with the provided index."""
        global ambiguous_matches, disambiguation_generator, disambiguation_canvas
        if (
            not ambiguous_matches
            or not disambiguation_generator
            or not disambiguation_canvas
        ):
            assert not ambiguous_matches
            assert not disambiguation_generator
            assert not disambiguation_canvas
            raise RuntimeError("Disambiguation not active")
        actions.mode.disable("user.gaze_ocr_disambiguation")
        disambiguation_canvas.close()
        disambiguation_canvas = None
        # Give the canvas a moment to disappear so it doesn't interfere with subsequent screenshots.
        actions.sleep("10ms")
        match = ambiguous_matches[index - 1]
        try:
            ambiguous_matches = disambiguation_generator.send(match)
            show_disambiguation()
        except StopIteration:
            # Execution completed successfully.
            reset_disambiguation()

    def hide_gaze_ocr_options():
        """Hide the disambiguation UI."""
        actions.mode.disable("user.gaze_ocr_disambiguation")
        reset_disambiguation()

    def click_text(text: TimestampedText):
        """Click on the provided on-screen text."""

        def run():
            yield from move_cursor_to_word_generator(text)
            actions.mouse_click(0)

        begin_generator(run())

    def double_click_text(text: TimestampedText):
        """Double-lick on the provided on-screen text."""

        def run():
            yield from move_cursor_to_word_generator(text)
            actions.mouse_click(0)
            actions.mouse_click(0)

        begin_generator(run())

    def right_click_text(text: TimestampedText):
        """Right-click on the provided on-screen text."""

        def run():
            yield from move_cursor_to_word_generator(text)
            actions.mouse_click(1)

        begin_generator(run())

    def middle_click_text(text: TimestampedText):
        """Middle-click on the provided on-screen text."""

        def run():
            yield from move_cursor_to_word_generator(text)
            actions.mouse_click(2)

        begin_generator(run())

    def modifier_click_text(modifier: str, text: TimestampedText):
        """Control-click on the provided on-screen text."""

        def run():
            yield from move_cursor_to_word_generator(text)
            actions.key(f"{modifier}:down")
            actions.mouse_click(0)
            actions.key(f"{modifier}:up")

        begin_generator(run())

    def change_text_homophone(text: TimestampedText):
        """Switch the on-screen text to a different homophone."""

        def run():
            # Use click instead of selection because it is more reliable.
            yield from move_cursor_to_word_generator(text)
            actions.mouse_click(0)
            actions.edit.select_word()
            actions.user.homophones_show_selection()

        begin_generator(run())
