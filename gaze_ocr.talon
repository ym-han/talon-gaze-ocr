mode: command
mode: dictation
-
(eye | i) (hover | [cursor] move): user.move_cursor_to_gaze_point()
(eye | i) [left] touch:
    user.move_cursor_to_gaze_point()
    mouse_click(0)
(eye | i) [left] dubclick:
    user.move_cursor_to_gaze_point()
    mouse_click(0)
    mouse_click(0)
(eye | i) right (touch | click):
    user.move_cursor_to_gaze_point()
    mouse_click(1)
(eye | i) middle (touch | click):
    user.move_cursor_to_gaze_point()
    mouse_click(2)
(eye | i) <user.modifiers> (touch | click):
    user.move_cursor_to_gaze_point()
    key("{modifiers}:down")
    mouse_click(0)
    key("{modifiers}:up")

(eye | i) scroll up:
    user.move_cursor_to_gaze_point(0, 40)
    user.mouse_scroll_up()
(eye | i) scroll up half:
    user.move_cursor_to_gaze_point(0, 40)
    user.mouse_scroll_up(0.5)
(eye | i) scroll down:
    user.move_cursor_to_gaze_point(0, -40)
    user.mouse_scroll_down()
(eye | i) scroll down half:
    user.move_cursor_to_gaze_point(0, -40)
    user.mouse_scroll_down(0.5)
(eye | i) scroll left:
    user.move_cursor_to_gaze_point(40, 0)
    user.mouse_scroll_left()
(eye | i) scroll left half:
    user.move_cursor_to_gaze_point(40, 0)
    user.mouse_scroll_left(0.5)
(eye | i) scroll right:
    user.move_cursor_to_gaze_point(-40, 0)
    user.mouse_scroll_right()
(eye | i) scroll right half:
    user.move_cursor_to_gaze_point(-40, 0)
    user.mouse_scroll_right(0.5)

ocr show [text]: user.show_ocr_overlay("text", 1)
ocr show boxes: user.show_ocr_overlay("boxes", 1)
(hover (seen | scene) | cursor move) <user.timestamped_prose>$: user.move_cursor_to_word(timestamped_prose)
(touch | click) <user.timestamped_prose>$:
    user.click_text(timestamped_prose)
dubclick <user.timestamped_prose>$:
    user.double_click_text(timestamped_prose)
right (touch | click) <user.timestamped_prose>$:
    user.right_click_text(timestamped_prose)
middle (touch | click) <user.timestamped_prose>$:
    user.middle_click_text(timestamped_prose)

<user.modifiers> (touch | click) <user.timestamped_prose>$:
    user.modifier_click_text(modifiers, timestamped_prose)

pre (seen | scene) <user.timestamped_prose>$: user.move_text_cursor_to_word(timestamped_prose, "before")
post (seen | scene) <user.timestamped_prose>$: user.move_text_cursor_to_word(timestamped_prose, "after")

select <user.prose_range>$:
    user.perform_ocr_action("select", "", prose_range)
{user.ocr_actions} [{user.ocr_modifiers}] (seen | scene) <user.prose_range>$:
    user.perform_ocr_action(ocr_actions, ocr_modifiers or "", prose_range)
replace [{user.ocr_modifiers}] [seen | scene] <user.prose_range> with <user.prose>$:
    user.replace_text(ocr_modifiers or "", prose_range, prose)
[go] pre <user.timestamped_prose> say <user.prose>$:
    user.insert_adjacent_to_text(timestamped_prose, "before", prose)
[go] post <user.timestamped_prose> say <user.prose>$:
    user.insert_adjacent_to_text(timestamped_prose, "after", prose)
phones (seen | scene) <user.timestamped_prose>$:
    user.change_text_homophone(timestamped_prose)

ocr tracker on: user.connect_ocr_eye_tracker()
ocr tracker off: user.disconnect_ocr_eye_tracker()
