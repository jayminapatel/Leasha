"""The settings controller: what happens when a control in Settings changes.

Layer: L5

Extracted from `app/ui/shell.py` (work order 202626082352 section 7,
"Structural"). Everything here was a `MainWindow` method: persisting a choice
to `index_state` or `.env`, applying it to the live engine, translator, tray or
view, and the small loaders that read those choices back at start-up.

**The window still owns the state.** The controller holds no settings of its
own - it reads and writes `window._settings`, `window._store` and the views
through `self._w`, exactly as the methods did through `self`. That is what
lets `_limits_changed` replace the frozen `Settings` in one place and every
other reader see it, and it is why `MainWindow` keeps a same-named method for
each handler below: signal wiring, tests and any other caller reach the same
behaviour by the same name, and a test that replaces `window._settings_changed`
still intercepts the calls made from in here, because they go back out
through the window rather than to a sibling method.

**Threading is unchanged.** A handler that starts a worker still does, with the
same `CallableWorker` and `run`; nothing here may touch the store or the disk
on the UI thread that the method did not already, and
`test_ui_never_blocks` scans this module with the same rules as the rest of
`app/ui`.

**Keyed state goes through `state_writes`** (bug 3a). Every `ui:*` choice
saved here used to be a synchronous `set_state`, which waits for the store's
write lock - the one an index run holds for every batch - so changing a
setting mid-run froze the window. They are queued on the ordered state writer
now; where something must follow the write (the Code tab re-reading its file
types) or a failure must be said out loud, that rides `on_saved`/`on_failed`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThreadPool
from PySide6.QtWidgets import QDialog

from app.core.errors import to_app_error
from app.core.logging import logger
from app.ui.state_writes import save_state, save_states
from app.ui.workers import CallableWorker, run

_log = logger.bind(component="ui.shell")


class SettingsController(QObject):
    """Settings persistence and application for one `MainWindow`.

    A `QObject` parented to the window, not a plain class: a signal connected
    to one of its bound methods is delivered on the GUI thread by the same
    rule that applied when the receiver was the window itself.
    """

    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self._w = window

    def _debug_recording_toggled(self, on: bool) -> None:
        """Remember the choice; it takes effect at the next start.

        Deliberately not applied to the running window. Turning recording on
        mid-session would produce a file that begins in the middle of whatever
        went wrong, missing the startup context that makes the rest readable -
        and the whole point of the feature is a file somebody else can follow
        from the top.
        """
        save_state(self._w._store, "ui:debug_recording", "on" if on else "off",
                   component="ui.settings")
        if on and not self._w.recorder.enabled:
            self._w.settings_view.environment.set_recording_status(
                "Recording starts the next time you open the app. "
                "The file goes in logs\\sessions\\."
            )
        elif not on and self._w.recorder.enabled:
            self._w.settings_view.environment.set_recording_status(
                f"Still recording to {self._w.recorder.path.name} until you close the app."
            )

    def _rerank_toggled(self, enabled: bool) -> None:
        """Apply the rerank switch now, and remember it.

        Live where it can be: the engine holds the reranker, and a quality
        setting that needs a restart to take effect is one people conclude does
        nothing. Persisted alongside, so the next launch agrees with the box.
        """
        reranker = getattr(self._w._engine, "reranker", None)
        if reranker is not None:
            try:
                reranker.enabled = bool(enabled)
            except Exception as exc:             # noqa: BLE001 - never fatal
                _log.warning("could not apply the rerank setting live: {}", exc)
        # **Whichever control was used, the other follows.** Signals are blocked
        # on the way in, or setting one would emit back into this handler and
        # the two would bounce off each other.
        self._w._set_toolbar_rerank(bool(enabled))
        # `getattr` on the window too: the toolbar's box can be toggled in the
        # beat before the Settings page is built (`_construct_deferred_pages`).
        settings_box = getattr(getattr(self._w, "settings_view", None), "rerank", None)
        if settings_box is not None and settings_box.isChecked() != bool(enabled):
            settings_box.blockSignals(True)
            settings_box.setChecked(bool(enabled))
            settings_box.blockSignals(False)
        save_state(self._w._store, "ui:rerank_enabled", "on" if enabled else "off",
                   component="ui.settings")

    def _set_toolbar_rerank(self, enabled: bool) -> None:
        """Show `enabled` on the search bar's box without re-emitting."""
        toggle = getattr(self._w.search_view, "rerank_toggle", None)
        if toggle is None or toggle.isChecked() == enabled:
            return
        toggle.blockSignals(True)
        toggle.setChecked(enabled)
        toggle.blockSignals(False)

    def _change_index_location(self) -> None:
        """Ask what to do about the index location, then record the decision.

        **Nothing is moved from here, and nothing is moved while the app is
        running.** The stores are open; copying a database out from underneath
        an open connection is how a half-copied index becomes the only index.
        So the decision is written down and applied by the installer path on the
        next start, which is the one moment nothing is holding the files.

        `.env` is written by `env_writer`, never by hand - that is the rule the
        settings work established, and this is the setting most able to do harm.
        """
        from app.ui.widgets.index_flows import ADOPT, FRESH, IndexLocationDialog

        if self._w.indexing_view.is_running():
            self._w.notify(
                "An index run is in progress. Stop it before moving the index.",
                8_000)
            return

        dialog = IndexLocationDialog(Path(self._w._settings.data_path), self._w)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        choice = dialog.choice()

        # **`.env` is NOT written here, and that is a correction.** It used to
        # be written immediately while the files stayed put - so the next start
        # opened an empty folder and an intact index became unreferenced. The
        # write and the move are one operation, performed together at startup by
        # `app.core.index_move`, before any store opens. Until then nothing has
        # changed and the application keeps working exactly as it did.
        try:
            from app.core.index_move import plan_move, write_pending

            plan_move(Path(self._w._settings.data_path), choice.destination, choice.action)
            write_pending(Path(self._w._settings.project_path), choice.action, choice.destination)
        except Exception as exc:                 # noqa: BLE001
            self._w._show_error(to_app_error(exc, "ui.settings"))
            return

        self._w.settings_view.data_path.setText(str(choice.destination))

        if choice.action == ADOPT:
            what = "will use the index already there"
        elif choice.action == FRESH:
            what = "will start a new, empty index there"
        else:
            what = "will move the index there, which can take a while"
        self._w.notify(
            f"Saved: the app {what} when you restart it. Nothing has moved yet, "
            "and this index keeps working until then.", 12_000)

    def _change_meaning_model(self) -> None:
        """Confirm the cost of changing the embedding model, then record it."""
        from app.ui.widgets.index_flows import RebuildVectorsDialog

        if self._w.indexing_view.is_running():
            self._w.notify(
                "An index run is in progress. Stop it before changing the model.",
                8_000)
            return

        current_dim = int(getattr(self._w._settings, "embed_dim", 384) or 384)
        dialog = RebuildVectorsDialog(
            str(getattr(self._w._settings, "embed_model", "")),
            self._w._chunk_count(),
            self._w,
            current_dim=current_dim,
            # Where the meaning model is loaded from, so its Download row
            # fetches into the same folder (2026-09-29).
            model_cache=getattr(self._w._settings, "model_cache", None),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            from app.core.env_writer import apply_values

            # **Both keys, in one write.** Writing `EMBED_MODEL` alone left
            # `EMBED_DIM` describing the previous model, which is not a
            # settings inconsistency but a broken index: the store refuses
            # vectors of the wrong width, and the refusal arrives on the first
            # batch after the new model has been downloaded, naming a setting
            # the person never edited. `env_writer` writes the file atomically,
            # so the two cannot land apart.
            apply_values(Path(self._w._settings.env_file), {
                "EMBED_MODEL": dialog.chosen_model(),
                "EMBED_DIM": str(dialog.chosen_dim()),
            })
        except Exception as exc:                 # noqa: BLE001
            self._w._show_error(to_app_error(exc, "ui.settings"))
            return

        save_state(self._w._store, "index:rebuild_vectors", "pending",
                   component="ui.settings")
        if dialog.chosen_dim() != current_dim:
            self._w.notify(
                f"Saved - {dialog.chosen_model()} at {dialog.chosen_dim()} "
                "dimensions. The vector store is rebuilt from empty on the next "
                "index run, so meaning-based search returns nothing until it "
                "finishes. Keyword search is unaffected.", 20_000)
        else:
            self._w.notify(
                "Saved. Restart, then run an index to re-embed everything - search "
                "keeps working on the old vectors until it finishes.", 12_000)

    def _chunk_count(self) -> int:
        """How many chunks would have to be re-embedded. Never raises.

        A count over `chunks` is one indexed aggregate and runs once, in
        response to a deliberate click, to put a real number in front of a
        decision that costs hours. Zero if it cannot be read - the dialog then
        says "a few minutes", which is the honest thing to say when the size is
        unknown rather than a number that was guessed.
        """
        try:
            return int(self._w._store.stats().get("chunks_total", 0))
        except Exception as exc:                 # noqa: BLE001
            _log.debug("could not count chunks: {}", exc)
            return 0

    def _settings_changed(self, values: dict) -> None:
        """Write `.env` settings a panel has changed, and apply what applies now.

        **The application writes `.env`; the user never does.** That is the rule
        the settings work established, and the reason `env_writer` exists - it
        preserves comments and keys this build has never heard of, so a newer
        installer's settings survive an older window saving one number.

        Debounced upstream, so this runs once when somebody stops adjusting a
        control rather than once per notch.
        """
        if not values:
            return
        try:
            from app.core.env_writer import apply_values

            apply_values(Path(self._w._settings.env_file), values)
        except Exception as exc:                 # noqa: BLE001
            self._w._show_error(to_app_error(exc, "ui.settings"))
            return

        # §1a. **The search behaviours take effect on the very next search**,
        # not at the next launch. For a switch somebody has just turned off,
        # the difference is between a working control and one they conclude is
        # broken - and they would be right to.
        if any(key.startswith("SEARCH_") for key in values):
            for key, value in values.items():
                if key.startswith("SEARCH_"):
                    # `Settings` is frozen, so the live object cannot be
                    # updated - the preferences dictionary is built from these
                    # values instead, which is the same answer by a route that
                    # works. See task #238 for the frozen-Settings question.
                    self._w._settings_overrides[key.lower()] = value
            self._w._apply_search_preferences()

        # §3a: the shortcut is re-taken on the spot, because a combination
        # somebody has just typed and cannot try until the next launch is a
        # control they will conclude does not work.
        if any(key.startswith("MINI_SEARCH") for key in values):
            for key, value in values.items():
                if key.startswith("MINI_SEARCH"):
                    self._w._settings_overrides[key.lower()] = value
            self._w._apply_hotkey()

        # Order 0y §2c: the editor a code result opens in is read at the moment
        # of opening (`MainWindow._open_code_at`), so a choice just made applies
        # to the very next Enter.
        for key, value in values.items():
            if key.startswith("CODE_EDITOR"):
                self._w._settings_overrides[key.lower()] = value

        # Reranking is the one that can take effect without a restart, and the
        # one people most want to see change - the rest are read when the thing
        # that uses them next starts.
        reranker = getattr(self._w._engine, "reranker", None)
        if reranker is not None:
            for key, attribute in (("RERANK_TOP_N", "top_n"),
                                   ("RERANK_WINDOW_CHARS", "window_chars")):
                if key in values:
                    try:
                        setattr(reranker, attribute, int(values[key]))
                    except Exception as exc:     # noqa: BLE001 - never fatal
                        _log.debug("could not apply {} live: {}", key, exc)

        if "RERANK_MODEL" in values:
            self._w.notify(
                "Saved. The rerank model is loaded at startup, so it changes "
                "the next time the app opens.", 8_000)

    def _tray_changed(self, minimise: bool, close: bool) -> None:
        """Apply and persist the tray preferences.

        Installs the icon the moment either is switched on, and says so if the
        desktop has no tray - a preference that silently does nothing is worse
        than one that is not offered, and this one was previously both.
        """
        self._w.tray.minimise_to_tray = bool(minimise)
        self._w.tray.close_to_tray = bool(close)
        save_states(self._w._store, {
            "ui:tray_minimise": "on" if minimise else "off",
            "ui:tray_close": "on" if close else "off",
        }, component="ui.settings")

        if (minimise or close) and not self._w.tray.installed and not self._w.tray.install():
            self._w.tray.minimise_to_tray = self._w.tray.close_to_tray = False
            self._w.settings_view.minimise_to_tray.setChecked(False)
            self._w.settings_view.close_to_tray.setChecked(False)
            self._w.notify(
                "This desktop has no notification area, so the window will "
                "minimise normally.", 8_000)

    def _cloud_toggled(self, enabled: bool) -> None:
        """Remember whether to index cloud-only files.

        Read live when a run starts, so it always worked *for that run* - and
        reset to off at every launch, which looks exactly like a setting being
        ignored. In `index_state` rather than `.env`: it is a decision about how
        this window starts a run, and the walker takes it as a parameter.
        """
        from app.index.run_setup import CLOUD_SWITCH_STATE_KEY

        # Also read by `app.cli index` for the saved folders (2026-10-04).
        save_state(self._w._store, CLOUD_SWITCH_STATE_KEY, "on" if enabled else "off",
                   component="ui.settings")

    def _limits_changed(self, values: dict) -> None:
        r"""Persist the resource ceilings. They take effect on the next run.

        Not on the run in flight: changing the worker count mid-run would mean
        stopping and restarting threads that are holding files open, and the
        gain is a few minutes on a job measured in hours.

        **These were written to the wrong place, and so they did nothing.**
        Every ceiling here landed in `index_state` under `ui:index_memory_mb`
        and friends - and nothing anywhere read those keys. `limits_from_
        settings` reads `Settings`, which is built from `.env`, so the memory
        ceiling, the worker count, the CPU cap and the free-space floor were all
        adjustable, saved, reported as saved, and inert. Six controls with real
        consequences, none of which had any.

        It matters more at a terabyte than it did at 100GB: raising the memory
        ceiling is the difference between a run that pauses constantly and one
        that does not, and somebody who raised it and saw no change would
        reasonably conclude the governor is broken rather than that the setting
        never arrived.

        So it goes through `.env` like every other setting - non-negotiable 11 -
        and the in-memory `Settings` is updated too, so the *next run in this
        session* uses it rather than requiring a restart.
        """
        if not values:
            return
        # `current_limits` keys are `Settings` field names, and the `.env` key
        # is the same name upper-cased - which is not a coincidence, it is how
        # `config.load_settings` reads them. Asserted by `test_settings_registry`.
        self._w._settings_changed({key.upper(): value for key, value in values.items()})
        # **`Settings` is frozen** (see ~line 128 and the module docstring), so
        # `setattr(self._settings, key, value)` always raised - every time,
        # for every key - and the `except` above caught it at DEBUG, where
        # nobody would ever see it. `.env` was written correctly; the live
        # object never changed, so the "next run in this session" this
        # function's own docstring promises never arrived without a restart.
        # `model_copy(update=...)` is this codebase's actual answer for a
        # frozen `Settings` (see `app.cli`'s `cmd_index`, which does the same
        # for `--rerank-model`): it produces a new instance with these fields
        # changed, and one replacement of the whole batch is what a frozen
        # model allows - there is no field-by-field mutation to fall back to.
        self._w._settings = self._w._settings.model_copy(update=values)
        self._w.notify("Saved. Applies to the next index run.", 5_000)

    def _ollama_model_changed(self, enabled: bool, model: str, timeout_s: int) -> None:
        """Apply a model choice immediately, and persist it.

        **Live, not on restart.** The client and the translator are mutated in
        place rather than rebuilt, so the choice takes effect on the very next
        press of Interpret - which matters because the natural next thing to do
        after choosing a model is to try it.

        The health cache is cleared: it was answered about the *old* model, and
        a stale "yes" would let a generate call proceed against a model that is
        not installed, failing several seconds later for no visible reason.
        """
        # 2026-10-04: a model chosen here wins over the one picked on the Search page.
        picker = getattr(self._w, "interpret_ctl", None)
        if picker is not None:
            picker.settings_chose(model)
        # 2026-10-04, code review: Settings' model goes to Settings' client, not to a
        # model picked on the Search page that is still in use.
        self._w._translator.reconfigure(
            model=model or None, timeout_s=float(timeout_s), enabled=enabled,
            model_client=getattr(picker, "default_client", None))
        save_states(self._w._store, {
            "ui:ollama_enabled": "on" if enabled else "off",
            "ui:ollama_model": model,
            "ui:ollama_timeout_s": str(int(timeout_s)),
        }, component="ui.settings")
        # The button appears and disappears with the setting, rather than
        # sitting there greyed out - an Interpret button that cannot interpret
        # is a permanent question with no answer on screen.
        self._w.search_view.set_interpret_enabled(enabled)
        self._w._warm_translator()
        self._w.notify(
            f"Interpret will use {model}, with up to {timeout_s}s." if enabled
            else "Query interpretation is off. Search is unaffected.", 8_000)

    def _apply_search_preferences(self) -> None:
        """Push the search behaviours to the surfaces that read them.

        Called at start-up and again whenever one is changed, so a switch takes
        effect on the very next search rather than at the next launch - which
        for a behaviour somebody has just switched off is the difference
        between a working control and one they believe is broken.
        """
        try:
            from app.search.policy import preferences

            # What was changed in this session wins over what was loaded at
            # start-up, which is the whole reason the overrides exist.
            found = preferences(self._w._settings, self._w._settings_overrides)
            # 2026-10-04: kept on the window as well, where the Files, Mail and
            # Code tabs (`ChipRow.reading`) and the mini box read them on every
            # search - they were given none, so a switch turned off here still
            # applied there.
            self._w.search_preferences = found
            self._w.search_view.set_search_preferences(found)
            # 2026-10-04, the owner: Files and Code read their dates in the
            # register the Search tab does (`presenter.facts.date_words`).
            from app.ui.presenter import set_date_register
            from app.ui.presenter.search import notice_register_for

            set_date_register(notice_register_for("search", found))
        except Exception as exc:                 # noqa: BLE001 - never fatal
            _log.debug("the search behaviours could not be applied: {}", exc)

    def _apply_hotkey(self) -> None:
        r"""Take, or give back, the global shortcut. **Never raises.**

        Called at start-up and whenever the setting changes. The result is
        pushed back into Settings as a sentence, because a shortcut the
        operating system refused is otherwise indistinguishable from one that
        works - and this is the feature the product is demonstrated with.
        """
        try:
            from app.ui.hotkey import HotkeyListener

            overrides = self._w._settings_overrides
            wanted = overrides.get(
                "mini_search_enabled",
                getattr(self._w._settings, "mini_search_enabled", True))
            text = str(overrides.get(
                "mini_search_hotkey",
                getattr(self._w._settings, "mini_search_hotkey", "")) or "")

            if self._w._hotkey is None:
                self._w._hotkey = HotkeyListener()
            self._w._hotkey.stop()
            taken = (self._w._hotkey.start(text, self._w._summon_mini)
                     if wanted else False)
            box = getattr(self._w.settings_view, "search_behaviour", None)
            if box is not None and hasattr(box, "say_hotkey"):
                box.say_hotkey(text, registered=taken or not wanted)
        except Exception as exc:                 # noqa: BLE001 - see docstring
            _log.debug("could not set the global shortcut: {}", exc)

    def _theme_changed(self, preference: str) -> None:
        self._w._theme_preference = preference
        save_state(self._w._store, "ui:theme", preference, component="ui.settings")
        self._w._apply_theme()

    def _refresh_link_scheme(self) -> None:
        r"""Read whether `leasha://` is registered, off the UI thread.

        Adoptions 7a wrote `register`/`unregister` and gave the window no way
        to show or change either. A registry read is I/O, so it is a worker,
        and the checkbox stays disabled until it answers. Off Windows there is
        nothing to read: the box says so by staying disabled.
        """
        import os

        box = self._w.settings_view.environment
        if os.name != "nt":
            box.set_links_state(None)
            return
        from app.core.deeplink import is_registered

        # `when_done` rather than a bare `connect(lambda ...)`: the lambda would have
        # no receiver, so a registry read that lands after the box is gone would call
        # into its C++ half from inside a Qt slot. `finished` was already safe - a
        # bound method of a QObject is dropped when that object dies - and this gives
        # the failure path the same protection. See `app/ui/later.py`.
        from app.ui.later import when_done

        worker = CallableWorker(is_registered, component="ui.links.read")
        when_done(box, worker,
                  finished=box.set_links_state,
                  failed=lambda _e: box.set_links_state(None))
        run(QThreadPool.globalInstance(), worker)

    def _links_toggled(self, wanted: bool) -> None:
        r"""The Settings box was ticked or unticked: write the registry, then
        show what it now says - so a write that failed is not left looking done."""
        from app.core.deeplink import is_registered, set_registered

        box = self._w.settings_view.environment

        def change() -> bool:
            set_registered(bool(wanted))
            return is_registered()

        from app.ui.later import when_done

        worker = CallableWorker(change, component="ui.links.write")
        when_done(box, worker,
                  finished=box.set_links_state,
                  failed=lambda _e: self._w._refresh_link_scheme())
        run(QThreadPool.globalInstance(), worker)

    def _load_roots(self) -> list[str]:
        """Index roots persist in `index_state`, alongside the index they build.

        Settings that vanish on restart are not settings. They live with the
        index rather than in .env because they describe *this* index, and .env is
        written by the installer and would be overwritten by a repair run.
        """
        try:
            stored = self._w._store.get_state("ui:roots", "")
        except Exception:                        # noqa: BLE001
            return []
        return [root for root in (stored or "").split("|") if root]

    def _load_code_types(self) -> tuple:
        """Which file types the Code tab lists. See `app/core/code_types.py`."""
        from app.core.code_types import choice_from

        return choice_from(self._w._store)

    def _save_code_types(self, preset: str, groups: list) -> None:
        from app.core.code_types import STATE_KEY, dump_choice

        def not_saved(error: Any) -> None:
            _log.warning("code file types not saved: {}", error)
            self._w.notify(
                "That Code file-type choice was not saved.", 8_000)

        def saved() -> None:
            # The Code tab reads this per search, so it takes effect on the next
            # keystroke - but it is already on screen, so redraw it now.
            #
            # Guarded: Order 0r item 2b builds Code a beat after the window
            # appears, and changing this Settings control in that gap would
            # otherwise raise on an attribute that does not exist yet. Nothing
            # is lost - Code reads this from the store on its own next search
            # regardless of whether it is redrawn immediately here.
            #
            # **After the write, not beside it** (bug 3a): the write is queued
            # now, and a redraw that ran first would re-read the old choice.
            code_view = getattr(self._w, "code_view", None)
            if code_view is not None:
                code_view.refresh()

        save_state(self._w._store, STATE_KEY, dump_choice(preset, groups),
                   component="ui.settings", owner=self,
                   on_saved=saved, on_failed=not_saved)

    def _load_root_modes(self) -> dict:
        """Which folders the owner has declared static. See `index/archives.py`."""
        from app.index.archives import MODE_STATE_KEY, load_modes

        try:
            return load_modes(self._w._store.get_state(MODE_STATE_KEY, "") or "")
        except Exception as exc:                     # noqa: BLE001
            _log.debug("index root modes not read: {}", exc)
            return {}

    def _save_root_modes(self, modes: dict) -> None:
        from app.index.archives import MODE_STATE_KEY, dump_modes

        def not_saved(error: Any) -> None:
            # **Said out loud.** A mode that silently failed to save looks like
            # it worked until the next run walks 1.5TB anyway, and by then
            # nobody connects the two.
            _log.warning("index root modes not saved: {}", error)

        save_state(self._w._store, MODE_STATE_KEY, dump_modes(modes),
                   component="ui.settings", owner=self, on_failed=not_saved)

    def _load_cloud_content_roots(self) -> set:
        """202626270514 §2b: which folders may hydrate cloud placeholders.
        See `index/walker.py`."""
        from app.index.walker import CLOUD_CONTENT_STATE_KEY, load_cloud_content_roots

        try:
            return set(load_cloud_content_roots(
                self._w._store.get_state(CLOUD_CONTENT_STATE_KEY, "") or ""))
        except Exception as exc:                     # noqa: BLE001
            _log.debug("cloud content roots not read: {}", exc)
            return set()

    def _save_cloud_content_roots(self, roots: set) -> None:
        from app.index.walker import CLOUD_CONTENT_STATE_KEY, dump_cloud_content_roots

        def not_saved(error: Any) -> None:
            _log.warning("cloud content roots not saved: {}", error)
            self._w.notify(
                "That folder's Live/Archive setting was not saved.", 8_000)

        save_state(self._w._store, CLOUD_CONTENT_STATE_KEY,
                   dump_cloud_content_roots(roots),
                   component="ui.settings", owner=self, on_failed=not_saved)

    def _load_first_folders(self) -> list:
        """2026-09-29: "Index this folder first", in order. See
        `app/index/read_order.py`."""
        from app.index.read_order import FIRST_FOLDERS_STATE_KEY, load_first_folders

        try:
            return load_first_folders(
                self._w._store.get_state(FIRST_FOLDERS_STATE_KEY, "") or "")
        except Exception as exc:                     # noqa: BLE001
            _log.debug("folders to index first not read: {}", exc)
            return []

    def _save_first_folders(self, folders: list) -> None:
        from app.index.read_order import FIRST_FOLDERS_STATE_KEY, dump_first_folders

        def not_saved(error: Any) -> None:
            _log.warning("folders to index first not saved: {}", error)
            self._w.notify("Which folders to index first was not saved.", 8_000)

        save_state(self._w._store, FIRST_FOLDERS_STATE_KEY,
                   dump_first_folders(folders),
                   component="ui.settings", owner=self, on_failed=not_saved)

    def _file_types_saved(self, changes: dict) -> None:
        """A file-type mapping changed - bump the generation, then say so.

        Every other write that bumps the generation happens inside a
        `write()` block already, because it just changed rows the search
        cache is keyed on. This one is different: `FileTypesEditor.save()`
        writes a config file on disk, not a table, and a mapping change
        (a format switched on/off, a converter route added, a size cap
        changed) invalidates cached search results exactly the same way a
        document write does - stale results from before the change must
        not linger. `bump_generation()` is the entry point for exactly
        this: a cache-invalidating event with no natural write() to
        piggy-back on.

        **Off the UI thread**, same reasoning and the same `CallableWorker`
        shape as `_run_idle_optimize` just below: `bump_generation()` opens
        a real `write()` transaction, and this method runs on a signal
        straight from the settings page, so "cheap" here is still a stutter
        the person clicking Save would feel. The status message is not
        conditioned on the bump succeeding - it reports that the mapping
        itself saved, which already happened by the time this signal fires;
        a failed bump only means the *next* search, not this save, might
        briefly serve a stale cache, and `bump_generation()` already logs
        its own failures.
        """
        worker = CallableWorker(self._w._store.bump_generation, component="ui.file_types")
        run(QThreadPool.globalInstance(), worker)
        self._w.notify(
            f"File types saved - {len(changes)} differ from the defaults. "
            "They apply to the next index run.", 12_000)

    def _save_pst_backend(self, backend: str) -> None:
        # The key every index run reads it back from (`run_setup.apply_saved_
        # pst_backend`, in `Pipeline.run`) - 2026-10-04, so a run in a separate
        # process or from the command line reads a `.pst` the chosen way too.
        from app.index.run_setup import PST_BACKEND_STATE_KEY

        save_state(self._w._store, PST_BACKEND_STATE_KEY, backend,
                   component="ui.settings", owner=self,
                   on_failed=lambda error: _log.warning(
                       "PST backend choice not saved: {}", error))
        self._w._apply_pst_backend(backend)

    def _apply_pst_backend(self, backend: str) -> None:
        from app.extract.base import extractor_for

        extractor = extractor_for(Path("x.pst"))
        if extractor is not None:
            extractor.backend = backend

    # -- Remove on the folder list (2026-10-07, the owner) ---------------------
    #
    # "If a location is removed the data for that location should be removed,
    # ask confirmation at removal that the index data will be removed too - the
    # only way the list must reflect what is in the index." Count, ask, delete,
    # and only then take the row off: a removal that could not happen (an
    # index run holds the index) leaves the folder listed, with its data.

    def _remove_folders(self, folders: list[str]) -> None:
        from app.index.forget_folder import count_from_folders

        kept = self._kept_after(folders)
        worker = CallableWorker(count_from_folders, self._w._store, folders, kept,
                                component="ui.settings")
        worker.signals.finished.connect(
            lambda count: self._confirm_remove(folders, int(count or 0)))
        worker.signals.failed.connect(self._w._show_error)
        run(QThreadPool.globalInstance(), worker)

    def _kept_after(self, folders: list[str]) -> list[str]:
        from app.index.archives import normalise

        gone = {normalise(folder) for folder in folders}
        return [root for root in self._w.settings_view.roots_box.current_roots()
                if normalise(root) not in gone]

    def _confirm_remove(self, folders: list[str], count: int) -> None:
        box = self._w.settings_view.roots_box
        if not count:
            # Nothing of it is in the index - the list already says so.
            box.remove_roots(folders)
            return
        if not self.ask_remove(folders, count):
            return
        from app.core.run_lock import GUI
        from app.index.forget_folder import forget_folders

        worker = CallableWorker(
            forget_folders, self._w._store, self._w._vectors,
            getattr(self._w, "_image_vectors", None), folders, self._kept_after(folders),
            run_lock_owner=GUI, component="ui.settings")
        worker.signals.finished.connect(self._folders_removed)
        worker.signals.failed.connect(self._w._show_error)
        self._w.notify("Removing from the index…")
        run(QThreadPool.globalInstance(), worker)

    def ask_remove(self, folders: list[str], count: int) -> bool:
        """The question, with the count. Overridable so tests need no dialog."""
        from PySide6.QtWidgets import QMessageBox

        from app.ui.presenter import remove_folders_confirmation

        title, body = remove_folders_confirmation(folders, count)
        answer = QMessageBox.question(
            self._w, title, body,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Yes

    def _folders_removed(self, result: Any) -> None:
        from app.ui.presenter import folders_removed_message

        folders = list(result.get("folders") or [])
        if folders:
            self._w.settings_view.roots_box.remove_roots(folders)
        for view, method in (("files_view", "refresh_summary"), ("mail_view", "refresh")):
            refresh = getattr(getattr(self._w, view, None), method, None)
            if refresh is not None:
                refresh()
        self._w.notify(folders_removed_message(result), 20_000)
        self._check_leftovers()

    # -- what the index holds from no listed folder ------------------------------

    def _check_leftovers(self, *_ignored: Any) -> None:
        """Count, off the window's thread, what the index holds from folders no
        longer on the list, and show it under the list. On loading, whenever
        the list changes and after a run. The newest count wins."""
        from app.index.forget_folder import count_outside

        self._leftover_check = getattr(self, "_leftover_check", 0) + 1
        check = self._leftover_check
        listed = self._w.settings_view.roots_box.current_roots()
        worker = CallableWorker(count_outside, self._w._store, listed,
                                component="ui.settings")
        worker.signals.finished.connect(
            lambda count: self._show_leftovers(check, int(count or 0)))
        worker.signals.failed.connect(
            lambda error: _log.warning("could not count leftovers: {}", error))
        run(QThreadPool.globalInstance(), worker)

    def _show_leftovers(self, check: int, count: int) -> None:
        if check == getattr(self, "_leftover_check", 0):
            self._w.settings_view.roots_box.set_leftovers(count)

    def _remove_leftovers(self) -> None:
        from app.index.forget_folder import count_outside

        listed = self._w.settings_view.roots_box.current_roots()
        worker = CallableWorker(count_outside, self._w._store, listed,
                                component="ui.settings")
        worker.signals.finished.connect(
            lambda count: self._confirm_leftovers(listed, int(count or 0)))
        worker.signals.failed.connect(self._w._show_error)
        run(QThreadPool.globalInstance(), worker)

    def _confirm_leftovers(self, listed: list[str], count: int) -> None:
        if not count:
            self._w.settings_view.roots_box.set_leftovers(0)
            return
        if not self.ask_remove_leftovers(count):
            return
        from app.core.run_lock import GUI
        from app.index.forget_folder import forget_outside

        worker = CallableWorker(
            forget_outside, self._w._store, self._w._vectors,
            getattr(self._w, "_image_vectors", None), listed,
            run_lock_owner=GUI, component="ui.settings")
        worker.signals.finished.connect(self._folders_removed)
        worker.signals.failed.connect(self._w._show_error)
        self._w.notify("Removing from the index…")
        run(QThreadPool.globalInstance(), worker)

    def ask_remove_leftovers(self, count: int) -> bool:
        """The question, with the count. Overridable so tests need no dialog."""
        from PySide6.QtWidgets import QMessageBox

        from app.ui.presenter import remove_leftovers_confirmation

        title, body = remove_leftovers_confirmation(count)
        answer = QMessageBox.question(
            self._w, title, body,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Yes

    def _save_roots(self, roots: list[str]) -> None:
        try:
            # The key `app.cli index` reads when it is given no folders, so
            # the command line and the window index the same thing. Named
            # rather than spelled out twice - see `cli.ROOTS_STATE_KEY`.
            from app.cli import ROOTS_STATE_KEY
        except Exception as exc:                 # noqa: BLE001
            _log.warning("index roots not saved: {}", exc)
            return
        save_state(self._w._store, ROOTS_STATE_KEY, "|".join(roots),
                   component="ui.settings", owner=self,
                   on_failed=lambda error: _log.warning(
                       "index roots not saved: {}", error))

    # -- Mail archives: each read its own way, and read again (2026-10-07) -------
    #
    # The owner: "need a way for each pst file it can be configured how to
    # index outlook or direct ... there should be a reindex button on those
    # files". The list and the saved choices are read on a worker - at start,
    # after every run and after a clear - and a choice is queued on the state
    # writer like every other `ui:*` value. "Read again" is the forced one-file
    # run "Index this file now" already starts; "Clear and read again" counts,
    # asks, removes under the index run lock, and only then starts that run.

    def _load_mail_archives(self, *_ignored: Any) -> None:
        """Read every mail archive in the index and the saved per-archive
        choices, off the window's thread. The newest load wins."""
        self._archives_load = getattr(self, "_archives_load", 0) + 1
        load = self._archives_load
        worker = CallableWorker(_read_mail_archives, self._w._store,
                                component="ui.settings")
        worker.signals.finished.connect(
            lambda found: self._show_mail_archives(load, found))
        worker.signals.failed.connect(
            lambda error: _log.warning("could not list mail archives: {}", error))
        run(QThreadPool.globalInstance(), worker)

    def _show_mail_archives(self, load: int, found: Any) -> None:
        if load != getattr(self, "_archives_load", 0):
            return
        rows, choices = found
        self._pst_choices = dict(choices or {})
        box = getattr(getattr(self._w, "settings_view", None), "mail_archives", None)
        if box is not None:
            box.set_archives(rows, self._pst_choices)

    def _save_archive_choice(self, path: str, backend: str) -> None:
        """One archive's way of being read. "auto" takes its own choice away."""
        from app.index.archives import normalise
        from app.index.run_setup import PST_BACKENDS_STATE_KEY, dump_pst_backends
        from app.ui.presenter import archive_choice_saved_message

        choices = dict(getattr(self, "_pst_choices", {}) or {})
        key = normalise(path)
        if str(backend or "auto") == "auto":
            choices.pop(key, None)
        else:
            choices[key] = str(backend)
        self._pst_choices = choices

        def not_saved(error: Any) -> None:
            _log.warning("mail archive choice not saved: {}", error)
            self._w.notify("How that archive is read was not saved.", 8_000)

        save_state(self._w._store, PST_BACKENDS_STATE_KEY, dump_pst_backends(choices),
                   component="ui.settings", owner=self, on_failed=not_saved)
        self._w.notify(archive_choice_saved_message(path, backend, choices), 8_000)

    def _read_archive_again(self, path: str) -> None:
        """Read it from the start, over the top: nothing is removed first."""
        from app.ui.presenter import read_again_message

        box = getattr(getattr(self._w, "settings_view", None), "mail_archives", None)
        messages = box.messages_in(path) if box is not None else 0
        # Said first: a start that is refused says why, over the top of this.
        self._w.notify(read_again_message(path, messages), 12_000)
        self._w._index_file_now(path)

    def _clear_archive(self, path: str) -> None:
        """Count what reading it brought in, then ask (`_confirm_clear_archive`)."""
        from app.index.forget_folder import count_from_folders

        worker = CallableWorker(count_from_folders, self._w._store, [path],
                                component="ui.settings")
        worker.signals.finished.connect(
            lambda count: self._confirm_clear_archive(path, int(count or 0)))
        worker.signals.failed.connect(self._w._show_error)
        run(QThreadPool.globalInstance(), worker)

    def _confirm_clear_archive(self, path: str, count: int) -> None:
        if not count:
            # Nothing of it in the index: there is nothing to clear, so this
            # is "Read again" and needs no question.
            self._read_archive_again(path)
            return
        if not self.ask_clear_archive(path, count):
            return
        from app.core.run_lock import GUI
        from app.index.forget_folder import file_ids_from_folders, forget_ids
        from app.ui.presenter import clearing_archive_message

        store = self._w._store
        worker = CallableWorker(
            forget_ids, store, self._w._vectors,
            getattr(self._w, "_image_vectors", None),
            lambda: file_ids_from_folders(store, [path]),
            run_lock_owner=GUI, component="ui.settings")
        worker.signals.finished.connect(
            lambda removed: self._archive_cleared(path, removed))
        worker.signals.failed.connect(self._w._show_error)
        self._w.notify(clearing_archive_message(path, count))
        run(QThreadPool.globalInstance(), worker)

    def ask_clear_archive(self, path: str, count: int) -> bool:
        """The question, with the count. Overridable so tests need no dialog."""
        from PySide6.QtWidgets import QMessageBox

        from app.ui.presenter import clear_archive_confirmation

        title, body = clear_archive_confirmation(path, count)
        answer = QMessageBox.question(
            self._w, title, body,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Yes

    def _archive_cleared(self, path: str, removed: Any) -> None:
        from app.ui.presenter import archive_cleared_message

        for view, method in (("files_view", "refresh_summary"), ("mail_view", "refresh")):
            refresh = getattr(getattr(self._w, view, None), method, None)
            if refresh is not None:
                refresh()
        self._load_mail_archives()
        self._w.notify(archive_cleared_message(path, removed), 20_000)
        self._w._index_file_now(path)


def _read_mail_archives(store: Any) -> tuple[list, dict]:
    """Worker body: every mail archive in the index, and the saved choices."""
    from app.index.run_setup import PST_BACKENDS_STATE_KEY, load_pst_backends

    rows = store.mail_archives()
    choices = load_pst_backends(store.get_state(PST_BACKENDS_STATE_KEY, "") or "")
    return rows, dict(choices or {})
