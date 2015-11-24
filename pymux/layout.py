#!/usr/bin/env python
# encoding: utf-8
"""
"""
from __future__ import unicode_literals

from prompt_toolkit.filters import HasFocus, Condition
from prompt_toolkit.layout.containers import VSplit, HSplit, Window, FloatContainer, Float, ConditionalContainer, Container
from prompt_toolkit.layout.controls import TokenListControl, FillControl, UIControl, BufferControl
from prompt_toolkit.layout.toolbars import TokenListToolbar
from prompt_toolkit.layout.dimension import LayoutDimension as D
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import BeforeInput, AppendAutoSuggestion
from prompt_toolkit.layout.screen import Char, Screen
from prompt_toolkit.mouse_events import MouseEventTypes

from pygments.token import Token
import datetime
import six

from .commands.lexer import create_command_lexer
from .screen import DEFAULT_TOKEN
import pymux.arrangement as arrangement

__all__ = (
    'LayoutManager',
)


class Background(UIControl):  # XXX: maybe turn into Container object for performance.
    """
    The background, as displayed when a session has been attached several
    times, and the window of the client is bigger than the current pymux
    window.
    """
    def reset(self):
        pass

    def has_focus(self, cli):
        return False

    def create_screen(self, cli, width, height):
        default_char = Char(' ', Token.Background)
        dot = Char('.', Token.Background)

        screen = Screen(default_char, initial_width=width)

        for y in range(height):
            row = screen.data_buffer[y]

            for x in range(width):
                if (x + y) % 2 == 0:
                    row[x] = dot
        return screen


class PaneContainer(UIControl):
    """
    User control that takes the Screen from a pymux pane/process.
    This also handles mouse support.
    """
    def __init__(self, pymux, pane):
        self.pane = pane
        self.process = pane.process
        self.pymux = pymux

    def create_screen(self, cli, width, height):
        process = self.process
        process.set_size(width, height)
        return process.screen.pt_screen

    def has_focus(self, cli):
        return (cli.current_buffer_name != 'COMMAND' and
            self.pymux.arrangement.active_pane == self.pane)

    def mouse_handler(self, cli, mouse_event):
        process = self.process
        x = mouse_event.position.x
        y = mouse_event.position.y

        if not self.has_focus(cli):
            # Focus this process when the mouse has been clicked.
            self.pymux.arrangement.active_window.active_pane = self.pane
        else:
            # Already focussed, send event to application when it requested
            # mouse support.
            if process.screen.sgr_mouse_support_enabled:
                # Xterm SGR mode.
                ev, m = {
                    MouseEventTypes.MOUSE_DOWN: ('0', 'M'),
                    MouseEventTypes.MOUSE_UP: ('0', 'm'),
                    MouseEventTypes.SCROLL_UP: ('64', 'M'),
                    MouseEventTypes.SCROLL_DOWN: ('65', 'M'),
                }.get(mouse_event.event_type)

                self.process.write_input(
                    '\x1b[<%s;%s;%s%s' % (ev, x + 1, y + 1, m))

            elif process.screen.urxvt_mouse_support_enabled:
                # Urxvt mode.
                ev = {
                    MouseEventTypes.MOUSE_DOWN: 32,
                    MouseEventTypes.MOUSE_UP: 35,
                    MouseEventTypes.SCROLL_UP: 96,
                    MouseEventTypes.SCROLL_DOWN: 97,
                }.get(mouse_event.event_type)

                self.process.write_input(
                    '\x1b[%s;%s;%sM' % (ev, x + 1, y + 1))

            elif process.screen.mouse_support_enabled:
                # Fall back to old mode.
                if x < 96 and y < 96:
                    ev = {
                            MouseEventTypes.MOUSE_DOWN: 32,
                            MouseEventTypes.MOUSE_UP: 35,
                            MouseEventTypes.SCROLL_UP: 96,
                            MouseEventTypes.SCROLL_DOWN: 97,
                    }.get(mouse_event.event_type)

                    self.process.write_input('\x1b[M%s%s%s' % (
                        six.unichr(ev),
                        six.unichr(x + 33),
                        six.unichr(y + 33)))


class PaneWindow(Window):
    def __init__(self, pymux, arrangement_pane, process):
        self._process = process
        super(PaneWindow, self).__init__(
            content=PaneContainer(pymux, arrangement_pane),
            get_vertical_scroll=lambda window: process.screen.line_offset,
            allow_scroll_beyond_bottom=True,
        )

    def write_to_screen(self, cli, screen, mouse_handlers, write_position):
        super(PaneWindow, self).write_to_screen(cli, screen, mouse_handlers, write_position)

        # If reverse video is enabled for the whole screen.
        if self._process.screen.has_reverse_video:
            data_buffer = screen.data_buffer

            for y in range(write_position.ypos, write_position.ypos + write_position.height):
                row = data_buffer[y]

                for x in range(write_position.xpos, write_position.xpos + write_position.width):
                    char = row[x]
                    token = list(char.token or DEFAULT_TOKEN)

                    # The token looks like ('C', *attrs). Replace the value of the reverse flag.
                    if token and token[0] == 'C':
                        token[-1] = not token[-1] # Invert reverse value.
                        row[x] = Char(char.char, tuple(token))


class MessageToolbar(TokenListToolbar):
    """
    Pop-up (at the bottom) for showing error/status messages.
    """
    def __init__(self, pymux):
        def get_tokens(cli):
            if pymux.message:
                return [(Token.Message, pymux.message)]
            else:
                return []

        super(MessageToolbar, self).__init__(
                get_tokens,
                filter=Condition(lambda cli: pymux.message is not None))


class LayoutManager(object):
    def __init__(self, pymux):
        self.pymux = pymux
        self.body = VSplit([
            Window(TokenListControl(lambda cli: []))
        ])
        self.layout = self._create_layout()

        # Keep track of render information.
        self.pane_write_positions = {}
        self.body_write_position = None

    def _create_layout(self):
        def get_status_tokens(cli):
            result = []
            previous_window = self.pymux.arrangement.previous_active_window

            for i, w in enumerate(self.pymux.arrangement.windows):
                result.append((Token.StatusBar, ' '))

                if w == self.pymux.arrangement.active_window:
                    result.append((Token.StatusBar.Window.Active, '%i:%s*' % (i, w.name)))

                elif w == previous_window:
                    result.append((Token.StatusBar.Window, '%i:%s-' % (i, w.name)))

                else:
                    result.append((Token.StatusBar.Window, '%i:%s ' % (i, w.name)))

            return result

        def get_time_tokens(cli):
            return [
                (Token.StatusBar,
                datetime.datetime.now().strftime('%H:%M %d-%b-%y')),
                (Token.StatusBar, ' '),
            ]

        return FloatContainer(
            content=HSplit([
                # The main window.
                HighlightBorders(self, self.pymux, FloatContainer(
                    Window(content=Background()),
                    floats=[
                        Float(get_width=lambda: self.pymux.get_window_size().columns,
                              get_height=lambda: self.pymux.get_window_size().rows,
                              content=TraceBodyWritePosition(self.pymux, self.body)),
                    ])),

                # Bottom toolbars.
                ConditionalContainer(
                    content=Window(
                        height=D.exact(1),
                        content=BufferControl(
                            buffer_name='COMMAND',
                            default_char=Char(' ', Token.CommandLine),
                            lexer=create_command_lexer(self.pymux),
                            input_processors=[
                                BeforeInput.static(':', Token.CommandLine),
                                AppendAutoSuggestion(),
                            ])
                    ),
                    filter=HasFocus('COMMAND'),
                ),
                ConditionalContainer(
                    content=VSplit([
                        Window(
                            height=D.exact(1),
                            content=TokenListControl(get_status_tokens,
                                default_char=Char(' ', Token.StatusBar))),
                        Window(
                            height=D.exact(1), width=D.exact(20),
                            content=TokenListControl(get_time_tokens,
                                align_right=True,
                                default_char=Char(' ', Token.StatusBar)))
                    ]),
                    filter=~HasFocus('COMMAND'),
                )
            ]),
            floats=[
                Float(bottom=1, left=0,
                      content=MessageToolbar(self.pymux)),
                Float(xcursor=True,
                      ycursor=True,
                      content=CompletionsMenu(max_height=12)),
            ]
        )

    def update(self):
        """
        Synchronise the prompt_toolkit layout with the layout defined by the
        pymux Arrangement.
        """
        active_window = self.pymux.arrangement.active_window

        # When zoomed, only show the current pane, otherwise show all of them.
        if active_window.zoom:
            content = _create_container_for_process(self.pymux, active_window.active_pane, zoom=True)
        else:
            content = _create_split(self.pymux, self.pymux.arrangement.active_window.root)

        self.body.children = [content]

        self.pymux.invalidate()


def _create_split(pymux, split):
    """
    Create a prompt_toolkit `Container` instance for the given pymux split.
    """
    assert isinstance(split, (arrangement.HSplit, arrangement.VSplit))

    is_vsplit = isinstance(split, arrangement.VSplit)

    content = []

    def vertical_line():
        " Draw a vertical line between windows. (In case of a vsplit) "
        char = '│'
        content.append(HSplit([
                Window(
                   width=D.exact(1), height=D.exact(1),
                   content=FillControl(char, token=Token.TitleBar.Line)),
                Window(width=D.exact(1),
                       content=FillControl(char, token=Token.Line))
            ]))

    for i, item in enumerate(split):
        if isinstance(item, (arrangement.VSplit, arrangement.HSplit)):
            content.append(_create_split(pymux, item))
        elif isinstance(item, arrangement.Pane):
            content.append(_create_container_for_process(pymux, item))
        else:
            raise TypeError('Got %r' % (item,))

        if is_vsplit and i != len(split) - 1:
            vertical_line()

    # Create prompt_toolkit Container.
    return_cls = VSplit if is_vsplit else HSplit
    return return_cls(content)


def _create_container_for_process(pymux, arrangement_pane, zoom=False):
    """
    Create a `Container` with a titlebar for a process.
    """
    assert isinstance(arrangement_pane, arrangement.Pane)
    process = arrangement_pane.process

    def has_focus():
        return pymux.arrangement.active_pane == arrangement_pane

    def get_titlebar_token(cli):
        return Token.TitleBar.Focussed if has_focus() else Token.TitleBar

    def get_titlebar_name_token(cli):
        return Token.TitleBar.Name.Focussed if has_focus() else Token.TitleBar.Name

    def get_title_tokens(cli):
        token = get_titlebar_token(cli)
        name_token = get_titlebar_name_token(cli)
        result = [(token, ' ')]

        if zoom:
            result.append((Token.TitleBar.Zoom, ' Z '))
            result.append((token, ' '))

        if arrangement_pane.name:
            result.append((name_token, ' %s ' % arrangement_pane.name))
            result.append((token, ' '))

        return result + [
            (token.Title, '%s' % process.screen.title),
        ]

    return TracePaneWritePosition(pymux, arrangement_pane, HSplit([
        Window(
            height=D.exact(1),
            content=TokenListControl(
                get_title_tokens,
                get_default_char=lambda cli: Char(' ', get_titlebar_token(cli)))
        ),
        PaneWindow(pymux, arrangement_pane, process),
    ]))


class _ContainerProxy(Container):
    def __init__(self, content):
        self.content = content

    def reset(self):
        self.content.reset()

    def preferred_width(self, cli, max_available_width):
        return self.content.preferred_width(cli, max_available_width)

    def preferred_height(self, cli, width):
        return self.content.preferred_height(cli, width)

    def write_to_screen(self, cli, screen, mouse_handlers, write_position):
        self.content.write_to_screen(cli, screen, mouse_handlers, write_position)

    def walk(self):
        return self.content.walk()


_focussed_border_titlebar = Char('┃', Token.TitleBar.Line.Focussed)
_focussed_border_vertical = Char('┃', Token.Line.Focussed)
_focussed_border_horizontal = Char('━', Token.Line.Focussed)
_focussed_border_left_top = Char('┏', Token.Line.Focussed)
_focussed_border_right_top = Char('┓', Token.Line.Focussed)
_focussed_border_left_bottom = Char('┗', Token.Line.Focussed)
_focussed_border_right_bottom = Char('┛', Token.Line.Focussed)

_border_vertical = Char('│', Token.Line)
_border_horizontal = Char('─', Token.Line)
_border_left_bottom = Char('└', Token.Line)
_border_right_bottom = Char('┘', Token.Line)
_border_left_top = Char('┌', Token.Line)
_border_right_top = Char('┐', Token.Line)


class HighlightBorders(_ContainerProxy):
    """
    Highlight the active borders. Happens post rendering.

    (We highlight the active pane when the rendering of everything else is
    done, otherwise, rendering of panes on the right will replace the result of
    this one.
    """
    def __init__(self, layout_manager, pymux, content):
        _ContainerProxy.__init__(self, content)
        self.pymux = pymux
        self.layout_manager = layout_manager

    def write_to_screen(self, cli, screen, mouse_handlers, write_position):
        # Clear previous list of pane coordinates.
        self.layout_manager.pane_write_positions = {}
        self.layout_manager.body_write_position = None

        # Render everything.
        _ContainerProxy.write_to_screen(self, cli, screen, mouse_handlers, write_position)

        # When rendering is done. Draw borders and highlight the borders of the
        # active pane.
        self._draw_borders(screen, write_position)

        active_pane = self.pymux.arrangement.active_pane
        try:
            pane_wp = self.layout_manager.pane_write_positions[self.pymux.arrangement.active_pane]
        except KeyError:
            pass
        else:
            self._highlight_active_pane(screen, pane_wp, write_position)

    def _draw_borders(self, screen, write_position):
        """
        Draw borders around the whole window. (When there is space.)
        """
        data_buffer = screen.data_buffer

        if self.layout_manager.body_write_position:
            wp = self.layout_manager.body_write_position

            # Bottom line.
            if wp.ypos + wp.height < write_position.ypos + write_position.height:
                row = data_buffer[wp.ypos + wp.height]

                for x in range(wp.xpos, wp.xpos + wp.width):
                    row[x] = _border_horizontal

                # Left/right bottom.
                data_buffer[wp.ypos + wp.height][wp.xpos - 1] = _border_left_bottom
                data_buffer[wp.ypos + wp.height][wp.xpos + wp.width] = _border_right_bottom

            # Left and right line.
            for y in range(wp.ypos + 1, wp.ypos + wp.height):
                data_buffer[y][wp.xpos - 1] = _border_vertical
                data_buffer[y][wp.xpos + wp.width] = _border_vertical

            # Left/right top
            data_buffer[wp.ypos][wp.xpos - 1] = _border_left_top
            data_buffer[wp.ypos][wp.xpos + wp.width] = _border_right_top

    def _highlight_active_pane(self, screen, pane_wp, write_position):
        " Highlight the current, active pane. "
        data_buffer = screen.data_buffer
        xpos, ypos, width, height = pane_wp.xpos, pane_wp.ypos, pane_wp.width, pane_wp.height

        xleft = xpos - 1
        xright = xpos + width

        # First line.
        row = data_buffer[ypos]

        if row[xleft].token == Token.Line:
            row[xleft] = _focussed_border_left_top
        else:
            row[xleft] = _focussed_border_titlebar

        if row[xright].token == Token.Line:
            row[xright] = _focussed_border_right_top
        else:
            row[xright] = _focussed_border_titlebar

        # Every following line.
        for y in range(ypos + 1, ypos + height):
            row = data_buffer[y]
            row[xleft] = row[xright] = _focussed_border_vertical

        # Draw the bottom line. (Only when there is space.)
        if ypos + height < write_position.ypos + write_position.height:
            row = data_buffer[ypos + height]

            for x in range(xpos, xpos + width):
                # Don't overwrite the titlebar of a pane below.
                if row[x].token == Token.Line:
                    row[x] = _focussed_border_horizontal

            # Bottom corners.
            row[xpos - 1] = _focussed_border_left_bottom
            row[xpos + width] = _focussed_border_right_bottom


class TracePaneWritePosition(_ContainerProxy):
    " Trace the write position of this pane. "
    def __init__(self, pymux, arrangement_pane, content):
        _ContainerProxy.__init__(self, content)

        self.pymux = pymux
        self.arrangement_pane = arrangement_pane

    def write_to_screen(self, cli, screen, mouse_handlers, write_position):
        _ContainerProxy.write_to_screen(self, cli, screen, mouse_handlers, write_position)

        self.pymux.layout_manager.pane_write_positions[self.arrangement_pane] = write_position


class TraceBodyWritePosition(_ContainerProxy):
    " Trace the write position of the whole body. "
    def __init__(self, pymux, content):
        _ContainerProxy.__init__(self, content)
        self.pymux = pymux

    def write_to_screen(self, cli, screen, mouse_handlers, write_position):
        _ContainerProxy.write_to_screen(self, cli, screen, mouse_handlers, write_position)
        self.pymux.layout_manager.body_write_position = write_position


def focus_left(pymux):
    " Move focus to the left. "
    _move_focus(pymux,
                lambda wp: wp.xpos - 2,  # 2 in order to skip over the border.
                lambda wp: wp.ypos)


def focus_right(pymux):
    " Move focus to the right. "
    _move_focus(pymux,
                lambda wp: wp.xpos + wp.width + 1,
                lambda wp: wp.ypos)


def focus_down(pymux):
    " Move focus down. "
    _move_focus(pymux,
                lambda wp: wp.xpos,
                lambda wp: wp.ypos + wp.height + 1)


def focus_up(pymux):
    " Move focus up. "
    _move_focus(pymux,
                lambda wp: wp.xpos,
                lambda wp: wp.ypos - 1)


def _move_focus(pymux, get_x, get_y):
    " Move focus of the active window. "
    window = pymux.arrangement.active_window

    write_pos = pymux.layout_manager.pane_write_positions[window.active_pane]
    x = get_x(write_pos)
    y = get_y(write_pos)

    # Look for the pane at this position.
    for pane, wp in pymux.layout_manager.pane_write_positions.items():
        if (wp.xpos <= x < wp.xpos + wp.width and
                wp.ypos <= y < wp.ypos + wp.height):
            window.active_pane = pane
            return
