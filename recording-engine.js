// ========================================
// RECORDING ENGINE MODULE
// ========================================
// Handles audio recording via MediaRecorder API,
// storage via IndexedDB, playback, and rolling cleanup.
// Exposes global RecordingEngine with init(), startRecording(),
// stopRecording(), saveRecording(), getRecordingsForStory(),
// getAllRecordings(), deleteRecording(), playRecording(),
// stopPlayback(), cleanupOldRecordings(), getStorageEstimate().
// ========================================

var RecordingEngine = (function() {
    // --- Constants ---
    var DB_NAME = 'ReadingQuestRecordings';
    var STORE_NAME = 'recordings';
    var DB_VERSION = 1;
    var MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000; // 7 days
    var CLEANUP_INTERVAL_MS = 60 * 60 * 1000;  // 1 hour
    var MAX_RECORDING_DURATION_S = 300;         // 5 minutes max per recording
    var MAX_BLOB_SIZE_BYTES = 5 * 1024 * 1024;  // 5 MB max per recording
    var MAX_TOTAL_STORAGE_MB = 50;              // 50 MB total cap for all recordings
    var AUDIO_BITRATE = 32000;                  // 32 kbps — plenty for speech, keeps files tiny

    // --- State ---
    var db = null;
    var mediaRecorder = null;
    var recordedChunks = [];
    var recordingStream = null;
    var recordingStartTime = 0;
    var state = 'idle'; // 'idle' | 'recording' | 'playing'
    var currentAudio = null;
    var cleanupTimer = null;
    var autoStopTimer = null;
    var mimeType = '';
    var supported = false; // set true during init if browser supports recording
    var audioContext = null;
    var analyserNode = null;

    // ========================================
    // INDEXEDDB SETUP
    // ========================================

    function openDatabase() {
        return new Promise(function(resolve, reject) {
            if (db) { resolve(db); return; }

            if (!window.indexedDB) {
                reject(new Error('IndexedDB is not supported in this browser.'));
                return;
            }

            var request = indexedDB.open(DB_NAME, DB_VERSION);

            request.onupgradeneeded = function(event) {
                var database = event.target.result;
                if (!database.objectStoreNames.contains(STORE_NAME)) {
                    var store = database.createObjectStore(STORE_NAME, { keyPath: 'id', autoIncrement: true });
                    store.createIndex('storyId', 'storyId', { unique: false });
                    store.createIndex('timestamp', 'timestamp', { unique: false });
                }
            };

            request.onsuccess = function(event) {
                db = event.target.result;
                resolve(db);
            };

            request.onerror = function(event) {
                console.error('RecordingEngine: Failed to open database', event.target.error);
                reject(event.target.error);
            };
        });
    }

    // ========================================
    // MIME TYPE DETECTION
    // ========================================

    function detectMimeType() {
        // Prefer webm/opus — best Chrome/Chromebook support and small file sizes.
        // audio/ogg intentionally excluded due to spotty Chromebook support.
        var types = [
            'audio/webm;codecs=opus',
            'audio/webm',
            'audio/mp4'
        ];
        for (var i = 0; i < types.length; i++) {
            if (typeof MediaRecorder !== 'undefined' && MediaRecorder.isTypeSupported(types[i])) {
                return types[i];
            }
        }
        return '';
    }

    // ========================================
    // RECORDING
    // ========================================

    function startRecording() {
        return new Promise(function(resolve, reject) {
            if (!supported) {
                reject(new Error('Recording is not available on this device. Your browser may not support audio recording.'));
                return;
            }

            if (state === 'recording') {
                reject(new Error('Already recording.'));
                return;
            }

            mimeType = detectMimeType();
            if (!mimeType) {
                reject(new Error('No supported audio format found in this browser.'));
                return;
            }

            navigator.mediaDevices.getUserMedia({ audio: true })
                .then(function(stream) {
                    recordingStream = stream;
                    recordedChunks = [];

                    try {
                        mediaRecorder = new MediaRecorder(stream, {
                            mimeType: mimeType,
                            audioBitsPerSecond: AUDIO_BITRATE // 32kbps — keeps files small for speech
                        });
                    } catch (e) {
                        cleanupStream();
                        reject(new Error('Failed to create MediaRecorder: ' + e.message));
                        return;
                    }

                    mediaRecorder.ondataavailable = function(event) {
                        if (event.data && event.data.size > 0) {
                            recordedChunks.push(event.data);
                        }
                    };

                    mediaRecorder.onerror = function(event) {
                        console.error('RecordingEngine: MediaRecorder error', event.error);
                        cleanupStream();
                        state = 'idle';
                    };

                    mediaRecorder.start(100); // collect data every 100ms
                    recordingStartTime = Date.now();
                    state = 'recording';

                    // Set up AudioContext + AnalyserNode for live level metering
                    try {
                        var AC = window.AudioContext || window.webkitAudioContext;
                        if (AC) {
                            audioContext = new AC();
                            analyserNode = audioContext.createAnalyser();
                            analyserNode.fftSize = 256;
                            var source = audioContext.createMediaStreamSource(stream);
                            source.connect(analyserNode);
                        }
                    } catch (e) {
                        console.warn('RecordingEngine: Could not create AudioContext for level metering:', e.message);
                        audioContext = null;
                        analyserNode = null;
                    }

                    // Auto-stop after max duration to prevent runaway recordings
                    if (autoStopTimer) clearTimeout(autoStopTimer);
                    autoStopTimer = setTimeout(function() {
                        if (state === 'recording') {
                            console.log('RecordingEngine: Auto-stopping recording after ' + MAX_RECORDING_DURATION_S + 's max duration.');
                            stopRecording().catch(function() {});
                        }
                    }, MAX_RECORDING_DURATION_S * 1000);

                    resolve();
                })
                .catch(function(err) {
                    var msg = 'Microphone access denied.';
                    if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
                        msg = 'Microphone permission was denied. To fix this: click the lock icon in the address bar, set Microphone to "Allow", then reload the page.';
                    } else if (err.name === 'NotFoundError') {
                        msg = 'No microphone found. Make sure your Chromebook\'s built-in microphone is working, or connect an external microphone.';
                    } else if (err.name === 'NotReadableError') {
                        msg = 'Microphone is in use by another app. Close other apps that might be using the microphone and try again.';
                    }
                    reject(new Error(msg));
                });
        });
    }

    function stopRecording() {
        return new Promise(function(resolve, reject) {
            if (state !== 'recording' || !mediaRecorder) {
                reject(new Error('Not currently recording.'));
                return;
            }

            // Clear auto-stop timer
            if (autoStopTimer) { clearTimeout(autoStopTimer); autoStopTimer = null; }

            var duration = Math.round((Date.now() - recordingStartTime) / 1000);

            mediaRecorder.onstop = function() {
                var blob = new Blob(recordedChunks, { type: mimeType });
                recordedChunks = [];
                cleanupStream();
                state = 'idle';

                // Check blob size — reject if too large
                if (blob.size > MAX_BLOB_SIZE_BYTES) {
                    console.warn('RecordingEngine: Recording too large (' + Math.round(blob.size / 1024) + ' KB). Discarding.');
                    reject(new Error('Recording is too large (' + Math.round(blob.size / 1024) + ' KB). Try a shorter reading.'));
                    return;
                }

                console.log('RecordingEngine: Recording captured — ' + duration + 's, ' + Math.round(blob.size / 1024) + ' KB');
                resolve({ blob: blob, duration: duration, mimeType: mimeType });
            };

            mediaRecorder.stop();
        });
    }

    function cleanupStream() {
        if (autoStopTimer) { clearTimeout(autoStopTimer); autoStopTimer = null; }
        // Clean up AudioContext and AnalyserNode
        if (analyserNode) {
            try { analyserNode.disconnect(); } catch (e) {}
            analyserNode = null;
        }
        if (audioContext) {
            try { audioContext.close(); } catch (e) {}
            audioContext = null;
        }
        if (recordingStream) {
            var tracks = recordingStream.getTracks();
            for (var i = 0; i < tracks.length; i++) {
                tracks[i].stop(); // releases mic — turns off Chromebook mic indicator light
            }
            recordingStream = null;
        }
        mediaRecorder = null;
    }

    // ========================================
    // STORAGE (SAVE / GET / DELETE)
    // ========================================

    function getTotalStorageUsed() {
        return openDatabase().then(function(database) {
            return new Promise(function(resolve, reject) {
                var tx = database.transaction(STORE_NAME, 'readonly');
                var store = tx.objectStore(STORE_NAME);
                var request = store.getAll();
                request.onsuccess = function() {
                    var total = 0;
                    var records = request.result || [];
                    for (var i = 0; i < records.length; i++) {
                        if (records[i].blob) total += records[i].blob.size || 0;
                    }
                    resolve(total);
                };
                request.onerror = function() { resolve(0); };
            });
        });
    }

    function getAllRecordingsSortedByTimestamp() {
        return openDatabase().then(function(database) {
            return new Promise(function(resolve, reject) {
                var tx = database.transaction(STORE_NAME, 'readonly');
                var store = tx.objectStore(STORE_NAME);
                var index = store.index('timestamp');
                var request = index.getAll();
                request.onsuccess = function() {
                    // Already sorted ascending by timestamp index
                    resolve(request.result || []);
                };
                request.onerror = function() { resolve([]); };
            });
        });
    }

    function autoDeleteOldest(neededBytes) {
        return getAllRecordingsSortedByTimestamp().then(function(recordings) {
            var totalBytes = 0;
            for (var i = 0; i < recordings.length; i++) {
                totalBytes += (recordings[i].blob ? recordings[i].blob.size : 0);
            }

            var targetMax = (MAX_TOTAL_STORAGE_MB * 1024 * 1024) - neededBytes;
            if (totalBytes <= targetMax) {
                return 0; // no deletions needed
            }

            // Delete oldest recordings one by one until we fit
            var deletions = [];
            var freedSoFar = 0;
            for (var j = 0; j < recordings.length; j++) {
                if (totalBytes - freedSoFar <= targetMax) break;
                var recSize = recordings[j].blob ? recordings[j].blob.size : 0;
                freedSoFar += recSize;
                deletions.push(deleteRecording(recordings[j].id));
            }

            return Promise.all(deletions).then(function() {
                console.log('RecordingEngine: Auto-deleted ' + deletions.length + ' oldest recording(s) to free space');
                return deletions.length;
            });
        });
    }

    function saveRecording(metadata, blob) {
        // Auto-delete oldest recordings if storage would exceed the cap
        return autoDeleteOldest(blob.size).then(function() {
            return openDatabase();
        }).then(function(database) {
            return new Promise(function(resolve, reject) {
                var record = {
                    storyId: metadata.storyId,
                    storyTitle: metadata.storyTitle || '',
                    level: metadata.level || '',
                    attemptNumber: metadata.attemptNumber || 1,
                    blob: blob,
                    mimeType: metadata.mimeType || mimeType || 'audio/webm',
                    timestamp: Date.now(),
                    duration: metadata.duration || 0,
                    wpm: metadata.wpm || 0
                };

                var tx = database.transaction(STORE_NAME, 'readwrite');
                var store = tx.objectStore(STORE_NAME);
                var request = store.add(record);

                request.onsuccess = function() {
                    resolve(request.result); // returns the auto-generated id
                };

                request.onerror = function(event) {
                    console.error('RecordingEngine: Failed to save recording', event.target.error);
                    reject(event.target.error);
                };
            });
        });
    }

    function getRecordingsForStory(storyId) {
        return openDatabase().then(function(database) {
            return new Promise(function(resolve, reject) {
                var tx = database.transaction(STORE_NAME, 'readonly');
                var store = tx.objectStore(STORE_NAME);
                var index = store.index('storyId');
                var request = index.getAll(storyId);

                request.onsuccess = function() {
                    var results = request.result || [];
                    // Sort by attemptNumber then timestamp
                    results.sort(function(a, b) {
                        if (a.attemptNumber !== b.attemptNumber) return a.attemptNumber - b.attemptNumber;
                        return a.timestamp - b.timestamp;
                    });
                    resolve(results);
                };

                request.onerror = function(event) {
                    console.error('RecordingEngine: Failed to get recordings for story', event.target.error);
                    reject(event.target.error);
                };
            });
        });
    }

    function getAllRecordings() {
        return openDatabase().then(function(database) {
            return new Promise(function(resolve, reject) {
                var tx = database.transaction(STORE_NAME, 'readonly');
                var store = tx.objectStore(STORE_NAME);
                var request = store.getAll();

                request.onsuccess = function() {
                    var results = request.result || [];
                    // Sort newest first
                    results.sort(function(a, b) {
                        return b.timestamp - a.timestamp;
                    });
                    resolve(results);
                };

                request.onerror = function(event) {
                    console.error('RecordingEngine: Failed to get all recordings', event.target.error);
                    reject(event.target.error);
                };
            });
        });
    }

    function deleteRecording(id) {
        return openDatabase().then(function(database) {
            return new Promise(function(resolve, reject) {
                var tx = database.transaction(STORE_NAME, 'readwrite');
                var store = tx.objectStore(STORE_NAME);
                var request = store.delete(id);

                request.onsuccess = function() {
                    resolve();
                };

                request.onerror = function(event) {
                    console.error('RecordingEngine: Failed to delete recording', event.target.error);
                    reject(event.target.error);
                };
            });
        });
    }

    // ========================================
    // PLAYBACK
    // ========================================

    function playRecording(id) {
        return openDatabase().then(function(database) {
            return new Promise(function(resolve, reject) {
                var tx = database.transaction(STORE_NAME, 'readonly');
                var store = tx.objectStore(STORE_NAME);
                var request = store.get(id);

                request.onsuccess = function() {
                    var record = request.result;
                    if (!record || !record.blob) {
                        reject(new Error('Recording not found.'));
                        return;
                    }

                    // Stop any current playback first
                    stopPlaybackInternal();

                    var url = URL.createObjectURL(record.blob);
                    currentAudio = new Audio(url);
                    state = 'playing';

                    currentAudio.onended = function() {
                        URL.revokeObjectURL(url);
                        state = 'idle';
                        currentAudio = null;
                    };

                    currentAudio.onerror = function() {
                        URL.revokeObjectURL(url);
                        state = 'idle';
                        currentAudio = null;
                        reject(new Error('Failed to play recording.'));
                    };

                    currentAudio.play().then(function() {
                        resolve(currentAudio);
                    }).catch(function(err) {
                        URL.revokeObjectURL(url);
                        state = 'idle';
                        currentAudio = null;
                        reject(new Error('Playback failed: ' + err.message));
                    });
                };

                request.onerror = function(event) {
                    reject(event.target.error);
                };
            });
        });
    }

    function stopPlaybackInternal() {
        if (currentAudio) {
            currentAudio.pause();
            currentAudio.currentTime = 0;
            // Revoke the object URL if we can find it
            if (currentAudio.src && currentAudio.src.indexOf('blob:') === 0) {
                URL.revokeObjectURL(currentAudio.src);
            }
            currentAudio = null;
        }
        state = 'idle';
    }

    function stopPlayback() {
        stopPlaybackInternal();
    }

    // ========================================
    // ROLLING CLEANUP (7-DAY EXPIRY)
    // ========================================

    function cleanupOldRecordings() {
        return openDatabase().then(function(database) {
            return new Promise(function(resolve, reject) {
                var cutoff = Date.now() - MAX_AGE_MS;
                var tx = database.transaction(STORE_NAME, 'readwrite');
                var store = tx.objectStore(STORE_NAME);
                var index = store.index('timestamp');
                var range = IDBKeyRange.upperBound(cutoff);
                var request = index.openCursor(range);
                var deletedCount = 0;

                request.onsuccess = function(event) {
                    var cursor = event.target.result;
                    if (cursor) {
                        cursor.delete();
                        deletedCount++;
                        cursor.continue();
                    } else {
                        if (deletedCount > 0) {
                            console.log('RecordingEngine: Cleaned up ' + deletedCount + ' recording(s) older than 7 days.');
                        }
                        resolve(deletedCount);
                    }
                };

                request.onerror = function(event) {
                    console.error('RecordingEngine: Cleanup error', event.target.error);
                    reject(event.target.error);
                };
            });
        });
    }

    // ========================================
    // STORAGE ESTIMATE
    // ========================================

    function getStorageEstimate() {
        if (navigator.storage && navigator.storage.estimate) {
            return navigator.storage.estimate().then(function(estimate) {
                return {
                    usage: estimate.usage || 0,
                    quota: estimate.quota || 0,
                    usageMB: Math.round((estimate.usage || 0) / (1024 * 1024) * 10) / 10,
                    quotaMB: Math.round((estimate.quota || 0) / (1024 * 1024) * 10) / 10
                };
            });
        }
        return Promise.resolve({ usage: 0, quota: 0, usageMB: 0, quotaMB: 0 });
    }

    // ========================================
    // STATE QUERY
    // ========================================

    function getAnalyserNode() {
        return analyserNode || null;
    }

    function getState() {
        return state;
    }

    function isSupported() {
        return typeof MediaRecorder !== 'undefined' &&
               typeof navigator.mediaDevices !== 'undefined' &&
               typeof navigator.mediaDevices.getUserMedia === 'function' &&
               typeof window.indexedDB !== 'undefined';
    }

    // ========================================
    // INIT
    // ========================================

    function init() {
        // Check browser compatibility before doing anything
        if (!isSupported()) {
            supported = false;
            console.warn('RecordingEngine: Browser does not support recording (MediaRecorder or getUserMedia unavailable). Recording features disabled.');
            return Promise.resolve();
        }

        // Verify a usable audio mime type exists
        if (!detectMimeType()) {
            supported = false;
            console.warn('RecordingEngine: No supported audio format found. Recording features disabled.');
            return Promise.resolve();
        }

        supported = true;

        return openDatabase().then(function() {
            // Run initial cleanup
            return cleanupOldRecordings();
        }).then(function() {
            // Schedule periodic cleanup every hour
            if (cleanupTimer) clearInterval(cleanupTimer);
            cleanupTimer = setInterval(function() {
                cleanupOldRecordings().catch(function(err) {
                    console.error('RecordingEngine: Periodic cleanup failed', err);
                });
            }, CLEANUP_INTERVAL_MS);

            console.log('RecordingEngine: Initialized (supported: true, format: ' + detectMimeType() + ').');
        }).catch(function(err) {
            console.error('RecordingEngine: Init failed', err);
        });
    }

    // ========================================
    // PUBLIC API
    // ========================================

    return {
        init: init,
        startRecording: startRecording,
        stopRecording: stopRecording,
        saveRecording: saveRecording,
        getRecordingsForStory: getRecordingsForStory,
        getAllRecordings: getAllRecordings,
        deleteRecording: deleteRecording,
        playRecording: playRecording,
        stopPlayback: stopPlayback,
        cleanupOldRecordings: cleanupOldRecordings,
        getStorageEstimate: getStorageEstimate,
        getState: getState,
        isSupported: isSupported,
        getAnalyserNode: getAnalyserNode
    };
})();
