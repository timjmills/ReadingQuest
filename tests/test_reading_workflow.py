"""Browser regressions: python3 -B -m unittest discover -s tests -v.

Requires Python Playwright and Chromium (already provided in the cloud environment).
Set READINGQUEST_CHROMIUM to use a different Chromium executable.
Speech completion and microphone responses are simulated; no device access is needed.
"""
import functools
import http.server
import os
from pathlib import Path
import threading
import unittest

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class ReadingWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(
            ('127.0.0.1', 0), functools.partial(QuietHandler, directory=str(ROOT)))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path=os.environ.get('READINGQUEST_CHROMIUM', '/usr/bin/chromium'),
            headless=True, args=['--no-sandbox', '--use-fake-device-for-media-stream',
                                 '--use-fake-ui-for-media-stream'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 1366, 'height': 600})
        self.context.add_init_script('''
            window.testSpeech = [];
            Object.defineProperty(window, 'speechSynthesis', {value: {
                getVoices: () => [], cancel: () => {}, pause: () => {}, resume: () => {},
                speak: utterance => window.testSpeech.push(utterance)
            }});
        ''')
        self.page = self.context.new_page()
        self.errors = []
        self.page.on('pageerror', lambda err: self.errors.append(str(err)))
        self.page.goto(self.url)
        self.mock_recording()

    def mock_recording(self):
        self.page.evaluate("""() => {
            window.originalRecordingMethods = Object.assign({}, RecordingEngine);
            RecordingEngine.startRecording = () => Promise.resolve();
            RecordingEngine.stopRecording = () => Promise.resolve({
                duration: 8, mimeType: 'audio/webm', blob: new Blob(['test audio'])
            });
            RecordingEngine.playRecording = () => {
                window.testRecordingAudio = new EventTarget();
                testRecordingAudio.pause = () => {};
                return Promise.resolve(testRecordingAudio);
            };
        }""")

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [], 'Unexpected browser script errors')

    def open_story(self, story_id=7):
        self.page.evaluate('(id) => { startSpecificStory(id); closeVocabModal(); }', story_id)
        self.assertTrue(self.page.locator('#practiceScreen').is_visible())

    def listen(self, highlight=False):
        self.page.locator('#storyActionBtn').click()
        self.page.evaluate('testSpeech[testSpeech.length - 1].onend()')

    def read(self, seconds=8):
        self.page.locator('#storyActionBtn').click()
        self.page.wait_for_function('timerRunning')
        self.page.evaluate('(seconds) => timerSeconds = seconds', seconds)
        self.page.locator('#storyActionBtn').click()
        self.page.wait_for_function('!recordingSaving && !timerRunning')

    def review(self):
        self.page.locator('#storyActionBtn').click()
        self.page.wait_for_function('storyReviewPlaying')
        self.page.evaluate("testRecordingAudio.dispatchEvent(new Event('ended'))")

    def assert_unlocked(self, expected=True):
        self.assertEqual(self.page.evaluate('questionsUnlocked'), expected)
        self.assertEqual(self.page.locator('#questionCard').evaluate(
            "e => e.classList.contains('locked')"), not expected)

    def test_read_once_and_listen_once_persist_across_reload_and_reentry(self):
        self.open_story()
        self.assert_unlocked(False)
        self.listen()
        self.assert_unlocked(False)
        self.read()
        self.assert_unlocked(False)
        self.review()
        self.assert_unlocked()
        self.page.evaluate('lockQuestions(); displayQuestion()')
        self.assert_unlocked()
        self.page.evaluate('closeStory()')
        self.open_story()
        self.assert_unlocked()
        self.page.reload()
        self.mock_recording()
        self.open_story()
        self.assert_unlocked()
        self.page.evaluate('closeStory()')
        self.open_story(13)
        self.assert_unlocked(False)

    def test_ordered_steps_and_saved_reading_resume_at_review(self):
        self.open_story()
        self.page.evaluate('startTimer()')
        self.assertFalse(self.page.evaluate('timerRunning'))
        self.listen()
        self.read()
        self.assert_unlocked(False)
        self.page.reload()
        self.mock_recording()
        self.open_story()
        self.page.wait_for_function('storyRecordingId !== null')
        self.assertEqual(self.page.evaluate('readingAttempts'), 1)
        self.page.evaluate("RecordingEngine.playRecording = () => { window.testRecordingAudio = new EventTarget(); testRecordingAudio.pause=()=>{}; return Promise.resolve(testRecordingAudio); }")
        self.review()
        self.assert_unlocked()

    def test_listening_progress_survives_reload(self):
        self.open_story()
        self.listen()
        self.page.reload()
        self.mock_recording()
        self.open_story()
        self.assertTrue(self.page.evaluate('storyListened'))
        self.read()
        self.assert_unlocked(False)
        self.review()
        self.assert_unlocked()

    def test_stopped_and_stale_speech_does_not_count_as_listening(self):
        self.open_story()
        self.page.locator('#storyActionBtn').click()
        self.page.evaluate('window.oldSpeech = testSpeech[testSpeech.length - 1]; stopSpeaking()')
        self.page.locator('#storyActionBtn').click()
        self.page.evaluate('oldSpeech.onend()')
        self.assertFalse(self.page.evaluate('storyListened'))
        self.page.evaluate('testSpeech[testSpeech.length - 1].onend()')
        self.assertTrue(self.page.evaluate('storyListened'))
        self.page.evaluate('closeStory()')
        self.open_story(13)
        self.page.locator('#storyActionBtn').click()
        self.page.evaluate('window.oldSpeech = testSpeech[testSpeech.length - 1]; closeStory()')
        self.open_story(19)
        self.page.evaluate('oldSpeech.onend()')
        self.assertFalse(self.page.evaluate('storyListened'))

    def test_short_read_does_not_count_and_leaving_stops_timer(self):
        self.open_story()
        self.listen()
        self.read(seconds=2)
        self.assert_unlocked(False)
        self.assertEqual(self.page.evaluate('readingAttempts'), 0)
        self.page.locator('.too-fast-ok-btn').click()
        self.page.locator('#storyActionBtn').click()
        self.page.evaluate('closeStory()')
        self.assertFalse(self.page.evaluate('timerRunning'))
        self.open_story()
        self.assertTrue(self.page.evaluate('storyListened'))
        self.read()
        self.assert_unlocked(False)
        self.review()
        self.assert_unlocked()

    def test_teacher_pin_cancel_wrong_correct_and_persistence(self):
        self.open_story()
        self.page.locator('#teacherSkipReadingBtn').click()
        self.page.locator('#teacherSkipDialog button').filter(has_text='Cancel').click()
        self.assert_unlocked(False)
        self.page.locator('#teacherSkipReadingBtn').click()
        self.page.locator('#teacherSkipPin').fill('0000')
        self.page.locator('#teacherSkipDialog button[type=submit]').click()
        self.assertIn('Incorrect PIN', self.page.locator('#teacherSkipError').inner_text())
        self.assert_unlocked(False)
        self.page.locator('#teacherSkipPin').fill('5773')
        self.page.locator('#teacherSkipDialog button[type=submit]').click()
        self.assert_unlocked()
        self.assertFalse(self.page.locator('#teacherSkipDialog').is_visible())
        self.assertEqual(self.page.locator('#teacherSkipPin').input_value(), '')
        self.page.reload()
        self.mock_recording()
        self.open_story()
        self.assert_unlocked()
        self.assertEqual(self.page.evaluate('readingAttempts'), 0, 'Skip must not invent a reading')

    def test_chromebook_answer_controls_visible_without_fullscreen(self):
        self.open_story()
        self.assertFalse(self.page.evaluate('!!document.fullscreenElement'))
        # Leaving fullscreen used to show a bottom banner over the answer controls.
        self.page.evaluate("document.dispatchEvent(new Event('fullscreenchange'))")
        self.assertTrue(self.page.locator('.fullscreen-prompt').evaluate("e => e.classList.contains('visible')"))
        self.page.evaluate('unlockQuestions()')
        for width, height in [(1366, 600), (1024, 500), (800, 450), (360, 480), (320, 480)]:
            self.page.set_viewport_size({'width': width, 'height': height})
            for dark in [False, True]:
                self.page.evaluate('(dark) => document.body.classList.toggle("dark-mode", dark)', dark)
                for scroll in [0, 100000]:
                    self.page.evaluate('(y) => window.scrollTo(0, y)', scroll)
                    for control in ['#skipBtn', '#checkBtn']:
                        box = self.page.locator(control).bounding_box()
                        self.assertGreaterEqual(box['x'], 0)
                        self.assertGreaterEqual(box['y'], 0)
                        self.assertLessEqual(box['x'] + box['width'], width)
                        self.assertLessEqual(box['y'] + box['height'], height)
                        self.assertTrue(self.page.locator(control).evaluate('''e => {
                            const r = e.getBoundingClientRect();
                            return e.contains(document.elementFromPoint(r.x + r.width/2, r.y + r.height/2));
                        }'''), f'{control} is covered at {width}x{height}')
                    self.assertFalse(self.page.evaluate(
                        'document.documentElement.scrollWidth > window.innerWidth'))
        # Wrong answer shows Next, also in the toolbar.
        self.page.evaluate('pickAnswer(currentAnswerMap.findIndex(i => i !== currentQuestions[0].correct)); checkAnswer()')
        self.assertTrue(self.page.locator('#nextBtn').is_visible())
        box = self.page.locator('#nextBtn').bounding_box()
        self.assertLessEqual(box['y'] + box['height'], 480)
        self.page.locator('#nextBtn').click()
        self.assertEqual(self.page.evaluate('questionIndex'), 1)

    def test_all_early_questions_have_two_choices_and_correct_scoring(self):
        self.open_story()
        result = self.page.evaluate('''() => {
            const early = stories.filter(s => ['aa','A','B','C'].includes(s.level));
            const bank = questions.filter(q => early.some(s => s.id === q.storyId));
            const failures = [];
            const savedTimeout = window.setTimeout;
            window.setTimeout = () => 0; // Score each question without auto-advance/celebration timers.
            launchConfetti = () => {};
            for (const story of early) {
                const qs = bank.filter(q => q.storyId === story.id);
                if (qs.length !== 8 || new Set(qs.map(q => q.category)).size !== 8) failures.push('coverage ' + story.id);
            }
            for (const q of bank) {
                if (q.type !== 'multiple' || q.options.length !== 2 || q.options[0] === q.options[1] ||
                    ![0,1].includes(q.correct) || !q.hint || !q.explanation) failures.push('shape ' + q.id);
                currentStoryId = q.storyId; questionsUnlocked = true; currentQuestions = [q]; questionIndex = 0;
                displayQuestion();
                if (document.querySelectorAll('#answerArea .answer-option').length !== 2) failures.push('render ' + q.id);
                correctCount = 0;
                pickAnswer(currentAnswerMap.indexOf(q.correct)); checkAnswer();
                if (correctCount !== 1 || !document.querySelector('.answer-option.correct')) failures.push('right ' + q.id);
                displayQuestion(); correctCount = 0;
                pickAnswer(currentAnswerMap.findIndex(i => i !== q.correct)); checkAnswer();
                if (correctCount !== 0 || !document.querySelector('.answer-option.incorrect')) failures.push('wrong ' + q.id);
            }
            window.setTimeout = savedTimeout;
            return {stories: early.length, questions: bank.length, failures};
        }''')
        self.assertEqual(result, {'stories': 46, 'questions': 368, 'failures': []})

    def test_delayed_recording_save_does_not_affect_another_story(self):
        self.open_story()
        self.listen()
        self.page.evaluate('''() => {
            window.recordingMetadata = null;
            RecordingEngine.stopRecording = () => new Promise(resolve => window.finishRecording = resolve);
            RecordingEngine.saveRecording = (metadata, blob) => {
                window.recordingMetadata = metadata; return Promise.resolve('test-recording');
            };
            timerRunning = true; timerSeconds = 8; isRecordingActive = true; doneReading();
        }''')
        self.assert_unlocked(False)
        self.page.evaluate('closeStory()')
        self.open_story(13)
        self.page.evaluate("finishRecording({duration:8,mimeType:'audio/webm',blob:new Blob(['test'])})")
        self.page.wait_for_function('recordingMetadata !== null')
        self.assertEqual(self.page.evaluate('recordingMetadata.storyId'), 7)
        self.assert_unlocked(False)
        self.assertEqual(self.page.locator('#listenBackPopup').count(), 0)
        self.assertEqual(self.page.locator('.attempt-play-btn').count(), 0)

    def test_late_recording_start_and_answer_advance_are_ignored_after_exit(self):
        self.open_story()
        self.listen()
        self.page.evaluate('''() => {
            RecordingEngine.isSupported = () => true;
            RecordingEngine.startRecording = () => new Promise(resolve => window.finishStart = resolve);
            window.stopCalls = 0;
            RecordingEngine.stopRecording = () => { stopCalls++; return Promise.resolve(); };
        }''')
        self.page.locator('#storyActionBtn').click()
        self.page.evaluate('closeStory()')
        self.page.evaluate('finishStart()')
        self.page.wait_for_function('stopCalls === 1')
        self.assertFalse(self.page.evaluate('isRecordingActive'))
        self.open_story()
        self.page.evaluate('unlockQuestions(); pickAnswer(currentAnswerMap.indexOf(currentQuestions[0].correct)); checkAnswer(); closeStory()')
        self.open_story(13)
        self.page.wait_for_timeout(1600)
        self.assertEqual(self.page.evaluate('questionIndex'), 0)
        self.assert_unlocked(False)

    def test_random_practice_restores_the_selected_story_progress(self):
        self.page.evaluate('''() => {
            storyReadingProgress[7] = {read: true, listened: true, unlocked: true};
            // Choose the first aa story deterministically, then use the normal practice entry.
            minLevel = 0; maxLevel = 0; selectedGenre = 'both'; selectedCategory = 'mixed';
            Math.random = () => 0; startPractice(); closeVocabModal();
        }''')
        self.assertEqual(self.page.evaluate('currentStoryId'), 7)
        self.assert_unlocked()

    def test_early_vocabulary_quizzes_also_use_two_choices(self):
        result = self.page.evaluate('''() => {
            const failures = [];
            for (const story of stories.filter(s => ['aa','A','B','C'].includes(s.level))) {
                startSpecificStory(story.id);
                if (!window._vocabQuizData.length) continue;
                startVocabQuiz();
                for (let i = 0; i < vocabQuizWords.length; i++) {
                    vocabQuizIndex = i; displayVocabQuizQuestion();
                    const buttons = [...document.querySelectorAll('.vocab-quiz-word-btn')];
                    if (buttons.length !== 2 || new Set(buttons.map(b => b.textContent)).size !== 2 ||
                        !buttons.some(b => b.textContent.toLowerCase() === vocabQuizWords[i].word.toLowerCase())) {
                        failures.push(story.id + ':' + i);
                    }
                }
                closeStory();
            }
            window._vocabQuizLevel = 'aa'; vocabQuizWords = [{word: 'cat', def: 'an animal'}];
            vocabQuizIndex = 0; displayVocabQuizQuestion();
            if (document.querySelectorAll('.vocab-quiz-word-btn').length !== 2) failures.push('single word');
            vocabQuizWords = []; displayVocabQuizQuestion();
            return failures;
        }''')
        self.assertEqual(result, [])

    def test_completed_story_keeps_questions_unlocked(self):
        self.open_story()
        self.listen()
        self.read()
        self.review()
        for index in range(6):
            self.page.evaluate('pickAnswer(currentAnswerMap.indexOf(currentQuestions[questionIndex].correct))')
            self.page.locator('#checkBtn').click()
            self.page.wait_for_function('(index) => questionIndex > index', arg=index)
        self.assertTrue(self.page.locator('#resultsScreen').is_visible())
        self.assertEqual(self.page.locator('#resultsScore').inner_text(), '100%')
        self.page.locator('#resultsScreen').get_by_role('button', name='🏠 Home', exact=True).click()
        self.open_story()
        self.assert_unlocked()

    def test_storage_failure_retains_current_session_and_reports_unsaved_progress(self):
        self.open_story()
        self.page.evaluate('''() => {
            const original = Storage.prototype.setItem;
            Storage.prototype.setItem = function(key, value) {
                if (key.startsWith('readingQuestReadingProgress')) throw new Error('storage full');
                return original.call(this, key, value);
            };
        }''')
        self.listen()
        self.read()
        self.assert_unlocked(False)
        self.review()
        self.assert_unlocked()
        self.page.evaluate('closeStory()')
        self.open_story()
        self.assert_unlocked()
        self.assertIn('Progress could not be saved', self.page.locator('#toast').inner_text())

    def test_native_recording_and_playback_unlock_only_after_review(self):
        self.page.evaluate('Object.assign(RecordingEngine, originalRecordingMethods)')
        self.open_story()
        self.listen()
        self.page.locator('#storyActionBtn').click()
        self.page.wait_for_function('isRecordingActive && timerRunning')
        self.page.wait_for_timeout(1200)
        self.page.evaluate('timerSeconds = 8')
        self.page.locator('#storyActionBtn').click()
        self.page.wait_for_function('storyRecordingId !== null && !recordingSaving')
        self.assert_unlocked(False)
        recordings = self.page.evaluate("""async () => (await RecordingEngine.getRecordingsForStory(7))
            .map(r => ({storyId: r.storyId, size: r.blob.size}))""")
        self.assertEqual(len(recordings), 1)
        self.assertEqual(recordings[0]['storyId'], 7)
        self.assertGreater(recordings[0]['size'], 0)
        self.page.locator('#storyActionBtn').click()
        self.page.wait_for_function('storyReviewComplete')
        self.assert_unlocked()

    def test_multiple_tabs_preserve_and_share_story_unlocks(self):
        self.open_story()
        other = self.context.new_page()
        other.on('pageerror', lambda err: self.errors.append(str(err)))
        other.goto(self.url)
        other.evaluate('startSpecificStory(7); closeVocabModal()')
        self.page.evaluate('readingAttempts = 1; storyReviewComplete = true; markStoryListened()')
        other.wait_for_function('questionsUnlocked')
        other.evaluate('closeStory(); startSpecificStory(13); closeVocabModal(); unlockQuestions()')
        self.page.evaluate('saveReadingProgress()')
        saved = self.page.evaluate("() => { refreshReadingProgress(); return storyReadingProgress; }")
        self.assertTrue(saved['7']['unlocked'])
        self.assertTrue(saved['13']['unlocked'])
        self.page.reload()
        self.mock_recording()
        self.open_story(13)
        self.assert_unlocked()
        other.close()

    def test_single_story_action_and_one_reading_graphic(self):
        self.open_story()
        self.assertEqual(self.page.locator('#readingProgress .attempt-box').count(), 1)
        def visible_actions():
            return self.page.locator('#storyContainer button:visible').count()
        def action_fits():
            for width in [1366, 800, 320]:
                self.page.set_viewport_size({'width': width, 'height': 600})
                box = self.page.locator('#storyActionBtn').bounding_box()
                self.assertGreaterEqual(box['x'], 0)
                self.assertLessEqual(box['x'] + box['width'], width)
                self.assertFalse(self.page.evaluate('document.documentElement.scrollWidth > innerWidth'))
            self.page.set_viewport_size({'width': 1366, 'height': 600})
        self.assertEqual(visible_actions(), 1)
        self.assertEqual(self.page.locator('#storyActionBtn .btn-text').inner_text(), 'Listen')
        action_fits()
        self.listen()
        self.assertEqual(visible_actions(), 1)
        self.assertEqual(self.page.locator('#storyActionBtn .btn-text').inner_text(), 'Read / Record')
        action_fits()
        self.read()
        self.assertEqual(visible_actions(), 1)
        self.assertTrue(self.page.locator('#attempt1').evaluate("e => e.classList.contains('completed')"))
        self.assertEqual(self.page.locator('#storyActionBtn .btn-text').inner_text(), 'Listen to my recording')
        action_fits()
        self.assert_unlocked(False)
        self.review()
        self.assert_unlocked()
        self.assertEqual(visible_actions(), 1)
        self.page.evaluate('closeStory()')
        self.open_story()
        self.assert_unlocked()
        self.assertEqual(visible_actions(), 1)
        self.assertEqual(self.page.locator('#readingProgress .attempt-box').count(), 1)
        self.assertEqual(self.page.locator('#storyContainer #printWorksheetBtn').count(), 0)
        self.assertEqual(self.page.locator('.teacher-reading-controls #printWorksheetBtn').count(), 1)
        self.page.evaluate('closeStory()')
        self.open_story(13)
        self.assertEqual(self.page.locator('#storyActionBtn .btn-text').inner_text(), 'Listen')
        self.assertFalse(self.page.locator('#attempt1').evaluate("e => e.classList.contains('completed')"))

    def test_text_size_controls_beside_dark_mode_resize_and_persist(self):
        self.open_story()
        self.assertEqual(self.page.locator('#practiceScreen #increaseTextBtn').count(), 0)
        self.assertEqual(self.page.locator('#increaseTextBtn').count(), 1)
        self.assertEqual(self.page.locator('#darkModeToggle').count(), 1)
        for width in [1366, 1024, 360, 320]:
            self.page.set_viewport_size({'width': width, 'height': 600})
            boxes = [self.page.locator(selector).bounding_box() for selector in
                     ['#decreaseTextBtn', '#increaseTextBtn', '#darkModeToggle']]
            self.assertLess(boxes[0]['x'], boxes[1]['x'])
            self.assertLess(boxes[1]['x'], boxes[2]['x'])
            self.assertEqual(len({round(box['y']) for box in boxes}), 1)
            self.assertGreaterEqual(boxes[0]['x'], 0)
            self.assertLessEqual(boxes[2]['x'] + boxes[2]['width'], width)
        self.page.set_viewport_size({'width': 1366, 'height': 600})
        selectors = ['#storyText', '#questionTextContent', '#answerArea .option-text']
        def sizes():
            return [float(self.page.locator(selector).first.evaluate(
                'e => parseFloat(getComputedStyle(e).fontSize)')) for selector in selectors]
        original = sizes()
        self.page.locator('#increaseTextBtn').click()
        enlarged = sizes()
        self.assertTrue(all(after > before for before, after in zip(original, enlarged)))
        self.page.locator('#decreaseTextBtn').click()
        self.assertEqual(sizes(), original)
        for _ in range(6):
            self.page.locator('#increaseTextBtn').click()
        self.assertTrue(self.page.locator('#increaseTextBtn').is_disabled())
        self.page.reload()
        self.mock_recording()
        self.open_story()
        self.assertTrue(self.page.locator('#increaseTextBtn').is_disabled())
        self.assertTrue(all(after > before for before, after in zip(original, sizes())))
        for _ in range(8):
            self.page.locator('#decreaseTextBtn').click()
        self.assertTrue(self.page.locator('#decreaseTextBtn').is_disabled())
        self.page.locator('#darkModeToggle').click()
        self.assertTrue(self.page.locator('body').evaluate("e => e.classList.contains('dark-mode')"))

    def test_vocabulary_ready_button_stays_visible_without_scrolling(self):
        for width, height in [(1366, 600), (1024, 500), (800, 450), (360, 480), (320, 480)]:
            self.page.set_viewport_size({'width': width, 'height': height})
            self.page.evaluate('startSpecificStory(7)')
            for dark in [False, True]:
                self.page.evaluate('(dark) => document.body.classList.toggle("dark-mode", dark)', dark)
                ready = self.page.locator('#vocabModal .vocab-preteach > .vocab-continue-btn')
                for scroll in [0, 100000]:
                    self.page.locator('.vocab-cards').evaluate('(e, y) => e.scrollTop = y', scroll)
                    box = ready.bounding_box()
                    self.assertGreaterEqual(box['y'], 0)
                    self.assertLessEqual(box['y'] + box['height'], height)
                    self.assertTrue(ready.evaluate('''e => {
                        const r = e.getBoundingClientRect();
                        return e.contains(document.elementFromPoint(r.x + r.width/2, r.y + r.height/2));
                    }'''), 'Ready button is covered')
            self.page.locator('#vocabModal .vocab-preteach > .vocab-continue-btn').click()
            self.assertTrue(self.page.locator('.vocab-quiz').is_visible())
            self.page.evaluate('cancelStoryFromVocab()')

    def test_final_listen_must_finish_and_cancel_does_not_unlock(self):
        self.open_story()
        self.listen()
        self.read()
        self.assert_unlocked(False)
        self.page.locator('#storyActionBtn').click()
        self.page.wait_for_function('storyReviewPlaying')
        self.page.evaluate('window.oldRecordingAudio = testRecordingAudio')
        self.page.locator('#storyActionBtn').click()  # Stop the recording partway through.
        self.assert_unlocked(False)
        self.page.evaluate("oldRecordingAudio.dispatchEvent(new Event('ended'))")
        self.assert_unlocked(False)
        self.review()
        self.assert_unlocked()
        self.page.reload()
        self.mock_recording()
        self.open_story()
        self.assert_unlocked()

    def test_microphone_and_save_failures_keep_retry_and_teacher_skip_available(self):
        self.open_story()
        self.listen()
        self.page.evaluate("() => { RecordingEngine.startRecording = () => Promise.reject(new Error('permission denied')); }")
        self.page.locator('#storyActionBtn').click()
        self.page.wait_for_function('!recordingStarting')
        self.assertFalse(self.page.evaluate('timerRunning'))
        self.assert_unlocked(False)
        self.assertIn('Microphone access', self.page.locator('#storyStepStatus').inner_text())
        self.assertTrue(self.page.locator('#teacherSkipReadingBtn').is_enabled())
        self.mock_recording()
        self.page.evaluate("() => { RecordingEngine.saveRecording = () => Promise.reject(new Error('storage full')); }")
        self.read()
        self.assert_unlocked(False)
        self.assertEqual(self.page.evaluate('readingAttempts'), 0)
        self.assertIn('could not be saved', self.page.locator('#storyStepStatus').inner_text())
        self.assertEqual(self.page.locator('#storyActionBtn .btn-text').inner_text(), 'Read / Record')

    def test_failed_playback_and_stale_playback_keep_required_review_locked(self):
        self.open_story()
        self.listen()
        self.read()
        self.page.evaluate("() => { RecordingEngine.playRecording = () => Promise.reject(new Error('cannot play')); }")
        self.page.locator('#storyActionBtn').click()
        self.page.wait_for_function('!storyReviewLoading')
        self.assert_unlocked(False)
        self.assertIn('could not play', self.page.locator('#storyStepStatus').inner_text())
        self.mock_recording()
        self.page.locator('#storyActionBtn').click()
        self.page.wait_for_function('storyReviewPlaying')
        self.page.evaluate('window.oldRecordingAudio = testRecordingAudio; closeStory()')
        self.open_story(13)
        self.page.evaluate("oldRecordingAudio.dispatchEvent(new Event('ended'))")
        self.assert_unlocked(False)
        self.assertFalse(self.page.evaluate('storyReviewComplete'))

    def test_previously_unlocked_story_stays_available_without_new_review(self):
        self.page.evaluate('''() => {
            storyReadingProgress[7] = {read: true, listened: true, unlocked: true};
            localStorage.setItem('readingQuestReadingProgress', JSON.stringify(storyReadingProgress));
        }''')
        self.page.reload()
        self.mock_recording()
        self.open_story()
        self.assert_unlocked()
        self.assertTrue(self.page.locator('#storyActionBtn').is_visible())
        self.assertIn('Optional', self.page.locator('#storyActionBtn').inner_text())

    def test_bottom_print_control_opens_the_current_story_worksheet(self):
        self.open_story()
        with self.page.expect_popup() as popup_info:
            self.page.locator('#printWorksheetBtn').click()
        popup = popup_info.value
        popup.wait_for_load_state()
        self.assertIn('The Lost Kitten', popup.locator('body').inner_text())
        popup.close()

    def test_completion_flags_restore_even_if_older_progress_is_malformed(self):
        self.open_story()
        self.listen()
        self.read()
        self.review()
        self.page.evaluate("localStorage.setItem('readingQuestReadingProgress', '{invalid')")
        self.page.reload()
        self.mock_recording()
        self.open_story()
        self.assert_unlocked()

    def test_vocabulary_correct_positions_are_mixed_and_scored_by_word(self):
        result = self.page.evaluate('''() => {
            const failures = [];
            const originalRandom = Math.random;
            const originalTimeout = window.setTimeout;
            Math.random = () => 0; // Answer placement must not follow question-order randomness.
            window.setTimeout = () => 0;
            for (const story of stories.filter(s => ['aa','A','B','C'].includes(s.level))) {
                startSpecificStory(story.id);
                if (!window._vocabQuizData.length) { closeStory(); continue; }
                startVocabQuiz();
                const positions = [];
                for (let i = 0; i < vocabQuizWords.length; i++) {
                    vocabQuizIndex = i; displayVocabQuizQuestion();
                    let buttons = [...document.querySelectorAll('.vocab-quiz-word-btn')];
                    const correct = vocabQuizWords[i].word;
                    const position = buttons.findIndex(b => b.textContent.toLowerCase() === correct.toLowerCase());
                    positions.push(position);
                    displayVocabQuizQuestion(); // Rerendering must keep the answer in the same position.
                    buttons = [...document.querySelectorAll('.vocab-quiz-word-btn')];
                    if (buttons.findIndex(b => b.textContent.toLowerCase() === correct.toLowerCase()) !== position) failures.push('unstable ' + story.id);
                    vocabQuizScore = 0; vocabQuizAttempts = {};
                    checkVocabQuizAnswer(correct, buttons[position]);
                    if (vocabQuizScore !== 1 || !buttons[position].classList.contains('vocab-quiz-correct')) failures.push('right ' + story.id);
                    displayVocabQuizQuestion();
                    buttons = [...document.querySelectorAll('.vocab-quiz-word-btn')];
                    const wrong = buttons[1 - position];
                    vocabQuizScore = 0; vocabQuizAttempts = {};
                    checkVocabQuizAnswer(wrong.textContent.toLowerCase(), wrong);
                    if (vocabQuizScore !== 0 || !wrong.classList.contains('vocab-quiz-wrong')) failures.push('wrong ' + story.id);
                }
                const left = positions.filter(p => p === 0).length;
                const right = positions.filter(p => p === 1).length;
                if (positions.length > 1 && (!left || !right || Math.abs(left - right) > 1)) failures.push('positions ' + story.id);
                closeStory();
            }
            Math.random = originalRandom; window.setTimeout = originalTimeout;
            return failures;
        }''')
        self.assertEqual(result, [])

    def test_early_story_answer_positions_are_mixed_with_correct_mapping(self):
        result = self.page.evaluate('''() => {
            const failures = [];
            const originalRandom = Math.random;
            const originalTimeout = window.setTimeout;
            Math.random = () => 0;
            window.setTimeout = () => 0;
            launchConfetti = () => {};
            for (const story of stories.filter(s => ['aa','A','B','C'].includes(s.level))) {
                startSpecificStory(story.id); closeVocabModal();
                questionsUnlocked = true;
                const positions = [];
                for (let i = 0; i < currentQuestions.length; i++) {
                    questionIndex = i; displayQuestion();
                    const q = currentQuestions[i];
                    const position = currentAnswerMap.indexOf(q.correct);
                    positions.push(position);
                    displayQuestion();
                    if (currentAnswerMap.indexOf(q.correct) !== position) failures.push('unstable ' + q.id);
                    correctCount = 0; pickAnswer(position); checkAnswer();
                    if (correctCount !== 1) failures.push('scoring ' + q.id);
                }
                if (positions.filter(p => p === 0).length !== 3 || positions.filter(p => p === 1).length !== 3) failures.push('positions ' + story.id);
                closeStory();
            }
            Math.random = originalRandom; window.setTimeout = originalTimeout;
            return failures;
        }''')
        self.assertEqual(result, [])

    def test_random_position_plans_do_not_require_strict_alternation(self):
        result = self.page.evaluate('''() => {
            const original = crypto.getRandomValues;
            crypto.getRandomValues = array => { array.fill(1); return array; };
            const first = buildAnswerPositionPlan(2, 6);
            crypto.getRandomValues = array => { array.fill(2); return array; };
            const second = buildAnswerPositionPlan(2, 6);
            crypto.getRandomValues = original;
            return {first, second};
        }''')
        self.assertEqual(result['first'].count(0), 3)
        self.assertEqual(result['first'].count(1), 3)
        self.assertTrue(any(a == b for a, b in zip(result['first'], result['first'][1:])))
        self.assertNotEqual(result['first'], result['second'])

    def assert_content_at_top(self, selector):
        self.page.wait_for_function('''selector => {
            const target = document.querySelector(selector);
            const toolbar = document.querySelector('.settings-bar');
            const offset = Math.max(0, toolbar.getBoundingClientRect().bottom) + 12;
            return document.activeElement === target && Math.abs(target.getBoundingClientRect().top - offset) < 2;
        }''', arg=selector)

    def test_vocabulary_and_story_steps_focus_text_then_unlocked_questions(self):
        self.page.evaluate('window.scrollTo(0, document.body.scrollHeight); startSpecificStory(28)')
        self.assertTrue(self.page.locator('#vocabModal').is_visible())
        self.page.evaluate('showVocabQuizResults()')
        self.page.locator('#vocabModal .vocab-continue-btn').click()
        self.assert_content_at_top('#storyText')
        self.page.locator('#storyActionBtn').click()
        self.assert_content_at_top('#storyText')
        self.page.evaluate('window.scrollBy(0, 100); testSpeech[testSpeech.length - 1].onend()')
        self.assert_content_at_top('#storyText')
        self.page.locator('#storyActionBtn').click()
        self.page.wait_for_function('timerRunning')
        self.assert_content_at_top('#storyText')
        # Students can scroll while reading; routine updates must not pull them back.
        self.page.evaluate('window.scrollBy(0, 100)')
        reading_position = self.page.evaluate('scrollY')
        self.page.evaluate('updateTimerDisplay(); updateStoryFlow()')
        self.page.wait_for_timeout(30)
        self.assertEqual(self.page.evaluate('scrollY'), reading_position)
        self.page.evaluate('timerSeconds = 8')
        self.page.locator('#storyActionBtn').click()
        self.page.wait_for_function('readingAttempts === 1 && !recordingSaving')
        self.assert_content_at_top('#storyText')
        self.page.locator('#storyActionBtn').click()
        self.page.wait_for_function('storyReviewPlaying')
        self.assert_content_at_top('#storyText')
        self.page.evaluate("testRecordingAudio.dispatchEvent(new Event('ended'))")
        self.assert_content_at_top('#questionCard')
        self.assert_unlocked()
        self.page.evaluate('pickAnswer(currentAnswerMap.findIndex(i => i !== currentQuestions[0].correct)); checkAnswer()')
        self.page.locator('#nextBtn').click()
        self.assert_content_at_top('#questionCard')
        self.page.evaluate('closeStory()')
        self.open_story(28)
        self.assert_content_at_top('#questionCard')

    def test_story_focus_accounts_for_wrapped_toolbar_on_small_windows(self):
        self.page.set_viewport_size({'width': 320, 'height': 480})
        self.open_story(28)
        self.assert_content_at_top('#storyText')
        self.listen()
        self.assert_content_at_top('#storyText')
        self.page.evaluate('unlockQuestions()')
        self.assert_content_at_top('#questionCard')

    def test_short_stories_have_larger_default_text_and_keep_manual_controls(self):
        story_ids = self.page.evaluate('''() => {
            const count = s => s.text.trim().split(/\\s+/).length;
            return [stories.find(s => count(s) <= 75).id,
                    stories.find(s => count(s) > 75 && count(s) <= 150).id,
                    stories.find(s => count(s) > 150).id];
        }''')
        sizes = []
        for story_id in story_ids:
            self.open_story(story_id)
            sizes.append(self.page.locator('#storyText').evaluate('e => parseFloat(getComputedStyle(e).fontSize)'))
            self.page.evaluate('closeStory()')
        self.assertGreater(sizes[0], sizes[1])
        self.assertGreater(sizes[1], sizes[2])
        self.open_story(story_ids[0])
        self.page.locator('#increaseTextBtn').click()
        self.assertGreater(self.page.locator('#storyText').evaluate('e => parseFloat(getComputedStyle(e).fontSize)'), sizes[0])

    def test_optional_rerecording_and_relistening_earn_saved_bonus_stars(self):
        self.open_story()
        self.listen()
        self.read()
        self.review()
        self.assertEqual(self.page.evaluate('totalStars'), 0)
        original_recording = self.page.evaluate('storyRecordingId')
        self.read()
        self.assert_unlocked()
        self.assertNotEqual(self.page.evaluate('storyRecordingId'), original_recording)
        self.assertEqual(self.page.evaluate('totalStars'), 1)
        self.assertEqual(self.page.locator('#optionalPracticeAction').input_value(), 'listen')
        self.review()
        self.assert_unlocked()
        self.assertEqual(self.page.evaluate('totalStars'), 2)
        self.review()  # A second full replay can earn another practice star.
        self.assertEqual(self.page.evaluate('totalStars'), 3)
        self.page.reload()
        self.mock_recording()
        self.open_story()
        self.page.wait_for_function('storyRecordingId !== null')
        self.assertEqual(self.page.evaluate('totalStars'), 3)
        self.page.locator('#optionalPracticeAction').select_option('listen')
        self.review()
        self.assertEqual(self.page.evaluate('totalStars'), 4)
        self.assert_unlocked()

    def test_cancelled_or_short_optional_practice_earns_no_bonus_and_never_relocks(self):
        self.open_story()
        self.listen()
        self.read()
        self.review()
        self.read(seconds=2)
        self.assert_unlocked()
        self.assertEqual(self.page.evaluate('totalStars'), 0)
        self.assertFalse(self.page.locator('#tooFastPopup').evaluate("e => e.classList.contains('show')"))
        self.page.locator('#optionalPracticeAction').select_option('listen')
        self.page.locator('#storyActionBtn').click()
        self.page.wait_for_function('storyReviewPlaying')
        self.page.evaluate('window.cancelledAudio = testRecordingAudio')
        self.page.locator('#storyActionBtn').click()
        self.page.evaluate("cancelledAudio.dispatchEvent(new Event('ended'))")
        self.assertEqual(self.page.evaluate('totalStars'), 0)
        self.assert_unlocked()

    def test_optional_rerecording_keeps_text_focused_then_returns_to_questions(self):
        self.open_story(28)
        self.listen()
        self.read()
        self.review()
        self.assert_content_at_top('#questionCard')
        self.page.locator('#storyActionBtn').click()
        self.page.wait_for_function('timerRunning')
        self.assert_content_at_top('#storyText')
        self.assert_unlocked()
        self.page.evaluate('timerSeconds = 8')
        self.page.locator('#storyActionBtn').click()
        self.page.wait_for_function('!recordingSaving && !timerRunning')
        self.assert_content_at_top('#questionCard')
        self.assert_unlocked()

    def test_star_awards_are_not_lost_between_tabs(self):
        other = self.context.new_page()
        other.goto(self.url)
        self.page.evaluate('awardEarnedStar()')
        other.evaluate('awardEarnedStar()')
        self.page.wait_for_function('totalStars === 2')
        other.wait_for_function('totalStars === 2')
        self.page.reload()
        self.mock_recording()
        self.assertEqual(self.page.evaluate('totalStars'), 2)
        other.close()


if __name__ == '__main__':
    unittest.main()
