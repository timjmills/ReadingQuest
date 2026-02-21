// ========================================
// FOCUS MONITOR MODULE
// ========================================
// Self-initializing module that hooks into the existing
// ReadingQuest app to track student focus during practice.
// Exposes global FocusMonitor with start(), stop(), getStats().
// ========================================

var FocusMonitor = (function() {
    // --- State ---
    var active = false;
    var activeTime = 0;       // seconds on-task
    var idleTime = 0;         // seconds idle (modal shown)
    var offTabTime = 0;       // seconds off-tab
    var totalElapsed = 0;     // total seconds since start

    var tickInterval = null;
    var idleTimeout = null;
    var IDLE_THRESHOLD = 30;  // seconds before idle prompt

    var isOffTab = false;
    var isIdle = false;
    var offTabStart = 0;
    var idleStart = 0;

    var eventLog = [];

    var idleStage = 0;        // 0=active, 1=nudge, 2=full idle
    var stage2Timeout = null;
    var streakSeconds = 0;     // current consecutive active seconds
    var longestStreak = 0;     // best streak this session
    var milestonesShown = {};  // track which milestones we've celebrated
    var idleEvents = 0;        // count of stage-2 idles
    var nudgeEvents = 0;       // count of stage-1 nudges

    // --- DOM element references (created once) ---
    var overlay = null;
    var idleBackdrop = null;
    var widget = null;
    var fullscreenPrompt = null;
    var wasTimerRunning = false;
    var wasTimerRunningIdle = false;
    var wasSpeechActive = false;
    var wasSpeechActiveIdle = false;

    // DOM refs for new elements
    var nudgeOverlay = null;
    var nudgeToast = null;
    var streakToast = null;

    // ========================================
    // DOM CREATION
    // ========================================

    function buildDOM() {
        // Tab-switch overlay
        overlay = document.createElement('div');
        overlay.className = 'focus-overlay';
        overlay.innerHTML =
            '<div class="focus-overlay-icon">&#128064;</div>' +
            '<div class="focus-overlay-text">Come back! Your reading is paused.</div>' +
            '<div class="focus-overlay-sub">Switch back to this tab to continue.</div>';
        document.body.appendChild(overlay);

        // Idle modal (stage 2)
        idleBackdrop = document.createElement('div');
        idleBackdrop.className = 'idle-modal-backdrop';
        idleBackdrop.innerHTML =
            '<div class="idle-modal">' +
                '<div class="idle-modal-icon">&#129300;</div>' +
                '<div class="idle-modal-title">Are you still reading?</div>' +
                '<div class="idle-modal-text">We haven\'t detected any activity for a while.</div>' +
                '<button class="idle-modal-btn" id="focusIdleBtn">Yes, I\'m reading!</button>' +
            '</div>';
        document.body.appendChild(idleBackdrop);

        // Floating timer widget
        widget = document.createElement('div');
        widget.className = 'focus-timer-widget';
        widget.innerHTML =
            '<div class="focus-timer-label">On Task</div>' +
            '<div class="focus-timer-time" id="focusTimerTime">00:00</div>' +
            '<div class="focus-timer-score" id="focusTimerScore">Focus: 100%</div>';
        document.body.appendChild(widget);

        // Fullscreen prompt bar
        fullscreenPrompt = document.createElement('div');
        fullscreenPrompt.className = 'fullscreen-prompt';
        fullscreenPrompt.innerHTML =
            '<span class="fullscreen-prompt-text">For the best focus, please use fullscreen mode</span>' +
            '<button class="fullscreen-prompt-btn" id="focusGoFullscreenBtn">Go Fullscreen</button>' +
            '<button class="fullscreen-prompt-dismiss" id="focusDismissFullscreenBtn">Dismiss</button>';
        document.body.appendChild(fullscreenPrompt);

        // Nudge overlay (subtle dim - stage 1)
        nudgeOverlay = document.createElement('div');
        nudgeOverlay.className = 'idle-nudge-overlay';
        document.body.appendChild(nudgeOverlay);

        // Nudge toast (top-center message)
        nudgeToast = document.createElement('div');
        nudgeToast.className = 'idle-nudge-toast';
        nudgeToast.innerHTML = '<span class="nudge-wave">👋</span> Are you there? Move your mouse or tap to continue!';
        document.body.appendChild(nudgeToast);

        // Streak celebration toast
        streakToast = document.createElement('div');
        streakToast.className = 'streak-toast';
        document.body.appendChild(streakToast);

        // Wire up buttons
        document.getElementById('focusIdleBtn').addEventListener('click', dismissIdle);
        document.getElementById('focusGoFullscreenBtn').addEventListener('click', requestFullscreen);
        document.getElementById('focusDismissFullscreenBtn').addEventListener('click', function() {
            fullscreenPrompt.classList.remove('visible');
        });
    }

    // ========================================
    // CORE TICK (runs every second)
    // ========================================

    function tick() {
        totalElapsed++;

        if (isOffTab) {
            offTabTime++;
        } else if (idleStage > 0) {
            idleTime++;
        } else {
            activeTime++;
            // Streak tracking
            streakSeconds++;
            if (streakSeconds > longestStreak) {
                longestStreak = streakSeconds;
            }
            checkStreakMilestone();
        }

        updateWidget();
    }

    // ========================================
    // STREAK MILESTONES
    // ========================================

    function checkStreakMilestone() {
        var milestones = [180, 300, 600]; // 3min, 5min, 10min
        for (var i = 0; i < milestones.length; i++) {
            if (streakSeconds === milestones[i] && !milestonesShown[milestones[i]]) {
                milestonesShown[milestones[i]] = true;
                showStreakCelebration(milestones[i]);
            }
        }
    }

    function showStreakCelebration(seconds) {
        var mins = Math.floor(seconds / 60);
        if (streakToast) {
            streakToast.textContent = '\uD83D\uDD25 ' + mins + ' min streak!';
            streakToast.classList.add('visible');
            setTimeout(function() {
                if (streakToast) streakToast.classList.remove('visible');
            }, 3000);
        }
    }

    // ========================================
    // WIDGET UPDATE
    // ========================================

    function updateWidget() {
        var mins = Math.floor(activeTime / 60);
        var secs = activeTime % 60;
        var timeStr = (mins < 10 ? '0' : '') + mins + ':' + (secs < 10 ? '0' : '') + secs;

        var timeEl = document.getElementById('focusTimerTime');
        var scoreEl = document.getElementById('focusTimerScore');
        if (timeEl) timeEl.textContent = timeStr;

        var score = totalElapsed > 0 ? Math.round((activeTime / totalElapsed) * 100) : 100;

        // Widget state
        if (widget) {
            var labelEl = widget.querySelector('.focus-timer-label');
            if (isOffTab || idleStage === 2) {
                widget.classList.add('paused');
                widget.classList.remove('nudge');
                if (labelEl) labelEl.textContent = 'Paused';
                if (scoreEl) scoreEl.textContent = 'Focus: ' + score + '%';
            } else if (idleStage === 1) {
                widget.classList.remove('paused');
                widget.classList.add('nudge');
                if (labelEl) labelEl.textContent = 'Are you there?';
                // Show idle duration
                var idleSecs = Math.round((Date.now() - idleStart) / 1000) || 0;
                var idleMins = Math.floor(idleSecs / 60);
                var idleS = idleSecs % 60;
                if (scoreEl) scoreEl.textContent = 'Idle: ' + idleMins + ':' + (idleS < 10 ? '0' : '') + idleS;
            } else {
                widget.classList.remove('paused');
                widget.classList.remove('nudge');
                // Show streak if >= 60s
                if (streakSeconds >= 60) {
                    var strkMins = Math.floor(streakSeconds / 60);
                    if (labelEl) labelEl.textContent = 'On Task';
                    if (scoreEl) scoreEl.textContent = '\uD83D\uDD25 ' + strkMins + 'min | Focus: ' + score + '%';
                } else {
                    if (labelEl) labelEl.textContent = 'On Task';
                    if (scoreEl) scoreEl.textContent = 'Focus: ' + score + '%';
                }
            }
        }
    }

    // ========================================
    // TAB VISIBILITY
    // ========================================

    function onVisibilityChange() {
        if (!active) return;

        if (document.hidden) {
            // Tab left
            isOffTab = true;
            offTabStart = Date.now();
            pauseAppTimer();
            if (overlay) overlay.classList.add('visible');
            logEvent('tab_away');
        } else {
            // Tab returned
            isOffTab = false;
            logEvent('tab_return');
            // Keep overlay visible for 1 second then hide and resume
            setTimeout(function() {
                if (overlay) overlay.classList.remove('visible');
                if (!isIdle) {
                    resumeAppTimer();
                }
            }, 1000);
        }
    }

    // Note: Speech pause/resume on tab-away is handled inside pauseAppTimer()
    // and resumeAppTimer() which are called by onVisibilityChange above.

    // ========================================
    // INACTIVITY DETECTION
    // ========================================

    function resetIdleTimer() {
        if (!active) return;
        if (idleStage > 0) return; // don't reset while nudge/idle is showing

        if (idleTimeout) {
            clearTimeout(idleTimeout);
            idleTimeout = null;
        }

        idleTimeout = setTimeout(function() {
            if (!active) return;
            if (isOffTab) return; // don't show idle if tab is away

            // Check practice screen is visible
            var ps = document.getElementById('practiceScreen');
            if (!ps || ps.classList.contains('hidden')) return;

            showNudge();
        }, IDLE_THRESHOLD * 1000);
    }

    function showNudge() {
        if (idleStage !== 0) return;
        idleStage = 1;
        nudgeEvents++;
        idleStart = Date.now();
        pauseAppTimerIdle();
        if (nudgeOverlay) nudgeOverlay.classList.add('visible');
        if (nudgeToast) nudgeToast.classList.add('visible');
        if (widget) widget.classList.add('nudge');
        logEvent('nudge_start');

        // Schedule stage 2 if no activity within IDLE_THRESHOLD * 2 more seconds
        stage2Timeout = setTimeout(function() {
            if (!active || isOffTab) return;
            showFullIdle();
        }, IDLE_THRESHOLD * 2 * 1000);
    }

    function showFullIdle() {
        idleStage = 2;
        idleEvents++;
        isIdle = true;
        idleStart = Date.now();
        // Hide nudge elements
        if (nudgeOverlay) nudgeOverlay.classList.remove('visible');
        if (nudgeToast) nudgeToast.classList.remove('visible');
        if (widget) widget.classList.remove('nudge');
        // Show full modal
        if (idleBackdrop) idleBackdrop.classList.add('visible');
        // Reset streak on full idle
        streakSeconds = 0;
        logEvent('idle_start');
    }

    function dismissIdle() {
        if (idleStage === 1) {
            // Dismiss nudge
            if (nudgeOverlay) nudgeOverlay.classList.remove('visible');
            if (nudgeToast) nudgeToast.classList.remove('visible');
            if (widget) widget.classList.remove('nudge');
            if (stage2Timeout) { clearTimeout(stage2Timeout); stage2Timeout = null; }
            logEvent('nudge_end');
        } else if (idleStage === 2) {
            // Dismiss full idle modal
            if (idleBackdrop) idleBackdrop.classList.remove('visible');
            logEvent('idle_end');
        }
        idleStage = 0;
        isIdle = false;
        resumeAppTimerIdle();
        resetIdleTimer();
    }

    function onUserActivity() {
        if (!active) return;
        if (idleStage === 1) {
            dismissIdle();
            return;
        }
        // Don't auto-dismiss stage 2 (requires button click)
        if (idleStage === 0) {
            resetIdleTimer();
        }
    }

    var activityEvents = ['mousemove', 'mousedown', 'click', 'scroll', 'keydown', 'touchstart'];

    function bindActivityListeners() {
        for (var i = 0; i < activityEvents.length; i++) {
            document.addEventListener(activityEvents[i], onUserActivity, { passive: true });
        }
    }

    function unbindActivityListeners() {
        for (var i = 0; i < activityEvents.length; i++) {
            document.removeEventListener(activityEvents[i], onUserActivity);
        }
    }

    // ========================================
    // APP TIMER BRIDGE (pause/resume existing timer)
    // ========================================

    function pauseAppTimer() {
        // Save whether the app timer was running so we only resume if it was
        wasTimerRunning = typeof timerRunning !== 'undefined' && timerRunning;
        if (wasTimerRunning) {
            if (typeof timerInterval !== 'undefined' && timerInterval) {
                clearInterval(timerInterval);
                timerInterval = null;
            }
            timerRunning = false;
        }
        // Pause TTS if it is currently speaking
        wasSpeechActive = false;
        if (window.speechSynthesis && window.speechSynthesis.speaking && !window.speechSynthesis.paused) {
            window.speechSynthesis.pause();
            wasSpeechActive = true;
            if (typeof pauseTimerHighlighting === 'function') {
                pauseTimerHighlighting();
            }
        }
    }

    function resumeAppTimer() {
        if (wasTimerRunning) {
            timerRunning = true;
            timerInterval = setInterval(function() {
                if (timerRunning) {
                    timerSeconds++;
                    if (typeof updateTimerDisplay === 'function') updateTimerDisplay();
                }
            }, 1000);
        }
        // Resume TTS if we paused it
        if (wasSpeechActive) {
            if (window.speechSynthesis) {
                window.speechSynthesis.resume();
            }
            if (typeof resumeTimerHighlighting === 'function') {
                resumeTimerHighlighting();
            }
            wasSpeechActive = false;
        }
    }

    function pauseAppTimerIdle() {
        wasTimerRunningIdle = typeof timerRunning !== 'undefined' && timerRunning;
        if (wasTimerRunningIdle) {
            if (typeof timerInterval !== 'undefined' && timerInterval) {
                clearInterval(timerInterval);
                timerInterval = null;
            }
            timerRunning = false;
        }
        // Pause TTS if it is currently speaking
        wasSpeechActiveIdle = false;
        if (window.speechSynthesis && window.speechSynthesis.speaking && !window.speechSynthesis.paused) {
            window.speechSynthesis.pause();
            wasSpeechActiveIdle = true;
            if (typeof pauseTimerHighlighting === 'function') {
                pauseTimerHighlighting();
            }
        }
    }

    function resumeAppTimerIdle() {
        if (wasTimerRunningIdle) {
            timerRunning = true;
            timerInterval = setInterval(function() {
                if (timerRunning) {
                    timerSeconds++;
                    if (typeof updateTimerDisplay === 'function') updateTimerDisplay();
                }
            }, 1000);
        }
        // Resume TTS if we paused it
        if (wasSpeechActiveIdle) {
            if (window.speechSynthesis) {
                window.speechSynthesis.resume();
            }
            if (typeof resumeTimerHighlighting === 'function') {
                resumeTimerHighlighting();
            }
            wasSpeechActiveIdle = false;
        }
    }

    // ========================================
    // FULLSCREEN
    // ========================================

    function requestFullscreen() {
        var el = document.documentElement;
        if (el.requestFullscreen) {
            el.requestFullscreen().catch(function() {});
        } else if (el.webkitRequestFullscreen) {
            el.webkitRequestFullscreen();
        } else if (el.msRequestFullscreen) {
            el.msRequestFullscreen();
        }
        if (fullscreenPrompt) fullscreenPrompt.classList.remove('visible');
    }

    function onFullscreenChange() {
        if (!active) return;
        var isFS = !!(document.fullscreenElement || document.webkitFullscreenElement || document.msFullscreenElement);
        if (!isFS) {
            // User exited fullscreen during practice
            if (fullscreenPrompt) fullscreenPrompt.classList.add('visible');
            logEvent('fullscreen_exit');
        } else {
            if (fullscreenPrompt) fullscreenPrompt.classList.remove('visible');
            logEvent('fullscreen_enter');
        }
    }

    // ========================================
    // EVENT LOG
    // ========================================

    function logEvent(type) {
        eventLog.push({
            type: type,
            time: new Date().toISOString(),
            elapsed: totalElapsed
        });
    }

    // ========================================
    // PUBLIC API
    // ========================================

    function start() {
        if (active) return;
        active = true;

        // Reset counters
        activeTime = 0;
        idleTime = 0;
        offTabTime = 0;
        totalElapsed = 0;
        isOffTab = false;
        isIdle = false;
        wasTimerRunning = false;
        wasTimerRunningIdle = false;
        wasSpeechActive = false;
        wasSpeechActiveIdle = false;
        eventLog = [];

        // Reset two-stage idle and streak state
        idleStage = 0;
        streakSeconds = 0;
        longestStreak = 0;
        milestonesShown = {};
        idleEvents = 0;
        nudgeEvents = 0;
        if (stage2Timeout) { clearTimeout(stage2Timeout); stage2Timeout = null; }

        // Build DOM elements if not yet created
        if (!overlay) buildDOM();

        // Show widget
        if (widget) widget.classList.add('visible');

        // Start tick
        tickInterval = setInterval(tick, 1000);

        // Bind listeners
        document.addEventListener('visibilitychange', onVisibilityChange);
        document.addEventListener('fullscreenchange', onFullscreenChange);
        document.addEventListener('webkitfullscreenchange', onFullscreenChange);
        bindActivityListeners();
        resetIdleTimer();

        // Request fullscreen
        requestFullscreen();

        logEvent('monitor_start');
    }

    function stop() {
        if (!active) return;
        active = false;

        // Stop tick
        if (tickInterval) {
            clearInterval(tickInterval);
            tickInterval = null;
        }

        // Clear idle timeout
        if (idleTimeout) {
            clearTimeout(idleTimeout);
            idleTimeout = null;
        }

        // Hide all UI
        if (overlay) overlay.classList.remove('visible');
        if (idleBackdrop) idleBackdrop.classList.remove('visible');
        if (widget) widget.classList.remove('visible');
        if (fullscreenPrompt) fullscreenPrompt.classList.remove('visible');
        if (nudgeOverlay) nudgeOverlay.classList.remove('visible');
        if (nudgeToast) nudgeToast.classList.remove('visible');
        if (streakToast) streakToast.classList.remove('visible');
        if (widget) widget.classList.remove('nudge');
        if (stage2Timeout) { clearTimeout(stage2Timeout); stage2Timeout = null; }

        // Unbind listeners
        document.removeEventListener('visibilitychange', onVisibilityChange);
        document.removeEventListener('fullscreenchange', onFullscreenChange);
        document.removeEventListener('webkitfullscreenchange', onFullscreenChange);
        unbindActivityListeners();

        // Exit fullscreen if active
        if (document.fullscreenElement || document.webkitFullscreenElement) {
            if (document.exitFullscreen) {
                document.exitFullscreen().catch(function() {});
            } else if (document.webkitExitFullscreen) {
                document.webkitExitFullscreen();
            }
        }

        logEvent('monitor_stop');
    }

    function getStats() {
        var score = totalElapsed > 0 ? Math.round((activeTime / totalElapsed) * 100) : 100;
        return {
            activeTime: activeTime,
            idleTime: idleTime,
            offTabTime: offTabTime,
            focusScore: score,
            longestStreak: longestStreak,
            idleEvents: idleEvents,
            nudgeEvents: nudgeEvents,
            eventLog: eventLog.slice()
        };
    }

    function setIdleThreshold(seconds) {
        IDLE_THRESHOLD = seconds;
        resetIdleTimer();
    }

    function resetIdle() {
        resetIdleTimer();
    }

    return {
        start: start,
        stop: stop,
        getStats: getStats,
        setIdleThreshold: setIdleThreshold,
        resetIdle: resetIdle
    };
})();
