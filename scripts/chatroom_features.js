/**
 * chatroom_features.js — File Upload & Code Execution features for Nova Chatroom
 *
 * Feature 1: File/Image Upload (drag-drop, paste, paperclip button)
 * Feature 2: Code Execution (Run buttons for AI code blocks)
 *
 * Written by Jordan Koch.
 */

(function() {
    'use strict';

    // --- Constants ---
    const AI_SENDERS_SET = new Set(['nova', 'claude code']);
    const IMAGE_EXTS = ['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'];
    const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50MB

    // --- DOM Elements (created dynamically if not present) ---
    let dropOverlay, imageModal, modalImg, uploadProgress, progressFill, progressText;
    let attachBtn, fileInput;

    function ensureElements() {
        // Drop overlay
        if (!document.getElementById('drop-overlay')) {
            dropOverlay = document.createElement('div');
            dropOverlay.id = 'drop-overlay';
            dropOverlay.innerHTML = '<div class="drop-icon">&#x1F4E5;</div><div class="drop-text">Drop file here</div>';
            document.body.appendChild(dropOverlay);
        } else {
            dropOverlay = document.getElementById('drop-overlay');
        }

        // Image modal
        if (!document.getElementById('image-modal')) {
            imageModal = document.createElement('div');
            imageModal.id = 'image-modal';
            modalImg = document.createElement('img');
            modalImg.id = 'modal-img';
            imageModal.appendChild(modalImg);
            document.body.appendChild(imageModal);
        } else {
            imageModal = document.getElementById('image-modal');
            modalImg = document.getElementById('modal-img');
        }

        // Upload progress
        if (!document.getElementById('upload-progress')) {
            uploadProgress = document.createElement('div');
            uploadProgress.id = 'upload-progress';
            uploadProgress.innerHTML = '<div class="progress-bar"><div class="progress-fill" id="progress-fill"></div></div><div class="progress-text" id="progress-text"></div>';
            const inputArea = document.getElementById('input-area');
            if (inputArea) inputArea.parentNode.insertBefore(uploadProgress, inputArea);
        } else {
            uploadProgress = document.getElementById('upload-progress');
        }
        progressFill = document.getElementById('progress-fill');
        progressText = document.getElementById('progress-text');

        // Attach button
        if (!document.getElementById('attach-btn')) {
            attachBtn = document.createElement('button');
            attachBtn.id = 'attach-btn';
            attachBtn.title = 'Attach file';
            attachBtn.innerHTML = '&#x1F4CE;';
            const inputArea = document.getElementById('input-area');
            if (inputArea) inputArea.insertBefore(attachBtn, inputArea.firstChild);
        } else {
            attachBtn = document.getElementById('attach-btn');
        }

        // Hidden file input
        if (!document.getElementById('file-input')) {
            fileInput = document.createElement('input');
            fileInput.type = 'file';
            fileInput.id = 'file-input';
            fileInput.style.display = 'none';
            document.body.appendChild(fileInput);
        } else {
            fileInput = document.getElementById('file-input');
        }
    }

    // --- Inject CSS ---
    function injectStyles() {
        if (document.getElementById('chatroom-features-css')) return;
        const style = document.createElement('style');
        style.id = 'chatroom-features-css';
        style.textContent = `
#drop-overlay {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(26, 26, 46, 0.92);
    z-index: 9999;
    align-items: center;
    justify-content: center;
    flex-direction: column;
    gap: 16px;
    border: 3px dashed #4fc3f7;
    pointer-events: none;
}
#drop-overlay.active { display: flex; }
#drop-overlay .drop-icon { font-size: 64px; opacity: 0.8; }
#drop-overlay .drop-text { font-size: 20px; color: #4fc3f7; font-weight: 600; }
#attach-btn {
    background: none;
    border: 1px solid #0f3460;
    border-radius: 8px;
    padding: 10px 12px;
    cursor: pointer;
    font-size: 18px;
    color: #888;
    transition: color 0.2s, border-color 0.2s;
    line-height: 1;
}
#attach-btn:hover { color: #4fc3f7; border-color: #4fc3f7; }
#upload-progress {
    display: none;
    padding: 4px 20px;
    background: #16213e;
}
#upload-progress .progress-bar {
    height: 3px;
    background: #0f3460;
    border-radius: 2px;
    overflow: hidden;
}
#upload-progress .progress-fill {
    height: 100%;
    background: #4fc3f7;
    width: 0%;
    transition: width 0.3s;
    border-radius: 2px;
}
#upload-progress .progress-text {
    font-size: 11px;
    color: #888;
    margin-top: 2px;
}
.file-card {
    display: flex;
    align-items: center;
    gap: 10px;
    background: #111827;
    border: 1px solid #1f2937;
    border-radius: 8px;
    padding: 10px 14px;
    margin-top: 4px;
    max-width: 360px;
    text-decoration: none;
    color: inherit;
    transition: border-color 0.2s;
}
.file-card:hover { border-color: #4fc3f7; }
.file-card .file-icon { font-size: 28px; flex-shrink: 0; }
.file-card .file-info { flex: 1; min-width: 0; }
.file-card .file-info .file-name { font-size: 13px; font-weight: 500; color: #e0e0e0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.file-card .file-info .file-meta { font-size: 11px; color: #888; margin-top: 2px; }
.file-card .file-download { font-size: 18px; color: #4fc3f7; flex-shrink: 0; }
.file-image-preview {
    margin-top: 6px;
    max-width: 400px;
    border-radius: 8px;
    overflow: hidden;
    cursor: pointer;
}
.file-image-preview img {
    max-width: 100%;
    max-height: 300px;
    display: block;
    border-radius: 8px;
    transition: transform 0.2s;
}
.file-image-preview img:hover { transform: scale(1.02); }
#image-modal {
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.9);
    z-index: 10000;
    align-items: center;
    justify-content: center;
    cursor: zoom-out;
}
#image-modal.active { display: flex; }
#image-modal img { max-width: 95vw; max-height: 95vh; border-radius: 4px; }
.code-block-wrapper {
    position: relative;
    margin-top: 6px;
    border-radius: 8px;
    overflow: hidden;
    background: #0d1117;
    border: 1px solid #1f2937;
}
.code-block-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 4px 10px;
    background: #161b22;
    border-bottom: 1px solid #1f2937;
    font-size: 11px;
    color: #888;
}
.code-block-lang { font-weight: 600; text-transform: uppercase; color: #4fc3f7; }
.code-block-actions { display: flex; gap: 6px; }
.code-block-actions button {
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 4px;
    padding: 3px 8px;
    font-size: 11px;
    cursor: pointer;
    color: #e0e0e0;
    transition: background 0.2s, border-color 0.2s;
}
.code-block-actions button:hover { background: #30363d; border-color: #4fc3f7; }
.code-block-actions .run-btn { color: #4caf50; border-color: #4caf50; }
.code-block-actions .run-btn:hover { background: #1b3d1b; }
.code-block-actions .request-run-btn { color: #ffb74d; border-color: #ffb74d; }
.code-block-actions .request-run-btn:hover { background: #3d2e1b; }
.code-block-pre {
    margin: 0;
    padding: 12px 14px;
    overflow-x: auto;
    font-family: 'SF Mono', 'Menlo', 'Monaco', 'Consolas', monospace;
    font-size: 12px;
    line-height: 1.5;
    color: #e0e0e0;
    white-space: pre;
    tab-size: 4;
}
.code-block-pre .kw { color: #ff7b72; }
.code-block-pre .str { color: #a5d6ff; }
.code-block-pre .num { color: #79c0ff; }
.code-block-pre .cmt { color: #8b949e; font-style: italic; }
.code-block-pre .fn { color: #d2a8ff; }
`;
        document.head.appendChild(style);
    }

    // --- Utility ---
    function escapeHtmlLocal(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    function formatFileSize(bytes) {
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
        return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
    }

    // --- Syntax Highlighting ---
    function highlightCode(code, lang) {
        let escaped = escapeHtmlLocal(code);
        if (lang === 'python' || lang === 'python3' || lang === 'bash' || lang === 'sh') {
            escaped = escaped.replace(/(#[^\n]*)/g, '<span class="cmt">$1</span>');
        } else if (lang === 'sql') {
            escaped = escaped.replace(/(--[^\n]*)/g, '<span class="cmt">$1</span>');
        }
        escaped = escaped.replace(/\b(\d+\.?\d*)\b/g, '<span class="num">$1</span>');
        if (lang === 'python' || lang === 'python3') {
            escaped = escaped.replace(/\b(def|class|import|from|return|if|elif|else|for|while|try|except|finally|with|as|yield|lambda|pass|break|continue|raise|in|not|and|or|is|None|True|False|async|await|print)\b/g, '<span class="kw">$1</span>');
        } else if (lang === 'bash' || lang === 'sh') {
            escaped = escaped.replace(/\b(if|then|else|elif|fi|for|do|done|while|until|case|esac|function|return|exit|echo|export|source|local|readonly)\b/g, '<span class="kw">$1</span>');
        } else if (lang === 'sql') {
            escaped = escaped.replace(/\b(SELECT|FROM|WHERE|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|TABLE|INDEX|JOIN|LEFT|RIGHT|INNER|OUTER|ON|AND|OR|NOT|IN|IS|NULL|AS|ORDER|BY|GROUP|HAVING|LIMIT|OFFSET|BEGIN|COMMIT|ROLLBACK|INTO|VALUES|SET)\b/gi, '<span class="kw">$1</span>');
        }
        escaped = escaped.replace(/\b([a-zA-Z_]\w*)\s*\(/g, '<span class="fn">$1</span>(');
        return escaped;
    }

    // --- Code Block Rendering ---
    // Override or augment the existing renderMessageText
    const _origRenderMessageText = window.renderMessageText;

    window.renderMessageText = function(text, msgId, sender) {
        // If called with old signature (just text), use original
        if (arguments.length === 1 && _origRenderMessageText) {
            // Check for code blocks first
            if (!text.includes('```')) {
                return _origRenderMessageText(text);
            }
        }

        const codeBlockRegex = /```(\w*)\n([\s\S]*?)```/g;
        let lastIndex = 0;
        let result = '';
        let match;
        let hasCodeBlock = false;
        const isAI = AI_SENDERS_SET.has((sender || '').toLowerCase());

        while ((match = codeBlockRegex.exec(text)) !== null) {
            hasCodeBlock = true;
            const before = text.slice(lastIndex, match.index);
            if (before.trim()) {
                let h = escapeHtmlLocal(before);
                h = h.replace(/@(Jordan|Nova|Claude|Jules|Colette|Gaston|Sam)\b/gi, '<span class="mention-highlight">@$1</span>');
                result += h;
            }
            const lang = match[1] || 'text';
            const code = match[2];
            const execLangs = ['python', 'python3', 'bash', 'sh', 'sql'];

            result += '<div class="code-block-wrapper">';
            result += '<div class="code-block-header">';
            result += '<span class="code-block-lang">' + escapeHtmlLocal(lang) + '</span>';
            result += '<div class="code-block-actions">';
            if (isAI && execLangs.includes(lang.toLowerCase())) {
                const codeEsc = btoa(unescape(encodeURIComponent(code)));
                result += '<button class="run-btn" data-msgid="' + msgId + '" data-code="' + codeEsc + '" data-lang="' + escapeHtmlLocal(lang) + '" onclick="window._executeCodeBtn(this)">Run</button>';
            } else if (!isAI && execLangs.includes(lang.toLowerCase())) {
                result += '<button class="request-run-btn" data-msgid="' + msgId + '" data-lang="' + escapeHtmlLocal(lang) + '" onclick="window._requestExecution(this)">Request Run</button>';
            }
            result += '</div></div>';
            result += '<pre class="code-block-pre">' + highlightCode(code, lang.toLowerCase()) + '</pre>';
            result += '</div>';
            lastIndex = match.index + match[0].length;
        }

        if (!hasCodeBlock) {
            if (_origRenderMessageText) return _origRenderMessageText(text);
            let html = escapeHtmlLocal(text);
            html = html.replace(/@(Jordan|Nova|Claude|Jules|Colette|Gaston|Sam)\b/gi, '<span class="mention-highlight">@$1</span>');
            return html;
        }

        const remaining = text.slice(lastIndex);
        if (remaining.trim()) {
            let h = escapeHtmlLocal(remaining);
            h = h.replace(/@(Jordan|Nova|Claude|Jules|Colette|Gaston|Sam)\b/gi, '<span class="mention-highlight">@$1</span>');
            result += h;
        }
        return result;
    };

    // --- File Rendering ---
    window.renderFileContent = function(msg) {
        const mime = msg.file_mime || '';
        const url = msg.file_url || '';
        const name = msg.file_name || 'file';
        const size = msg.file_size || 0;
        if (mime.startsWith('image/')) {
            return '<div class="file-image-preview" onclick="window._showImageModal(this.querySelector(\'img\').src)"><img src="' + escapeHtmlLocal(url) + '" alt="' + escapeHtmlLocal(name) + '" loading="lazy" /></div>' +
                '<div style="font-size:11px;color:#888;margin-top:2px;">' + escapeHtmlLocal(name) + ' (' + formatFileSize(size) + ')</div>';
        }
        let icon = '&#x1F4C4;';
        if (mime.includes('pdf')) icon = '&#x1F4D5;';
        else if (mime.includes('zip') || mime.includes('tar') || mime.includes('gzip')) icon = '&#x1F4E6;';
        else if (mime.includes('text') || mime.includes('markdown')) icon = '&#x1F4DD;';
        else if (name.match(/\.(py|js|ts|swift|rs|go|c|h|cpp|java|rb|sh)$/)) icon = '&#x1F4BB;';
        return '<a class="file-card" href="' + escapeHtmlLocal(url) + '" download="' + escapeHtmlLocal(name) + '" target="_blank">' +
            '<div class="file-icon">' + icon + '</div>' +
            '<div class="file-info"><div class="file-name">' + escapeHtmlLocal(name) + '</div>' +
            '<div class="file-meta">' + formatFileSize(size) + ' &middot; ' + escapeHtmlLocal(mime) + '</div></div>' +
            '<div class="file-download">&#x2B07;</div></a>';
    };

    // --- Code Execution ---
    window._executeCodeBtn = function(btn) {
        if (!window.ws || window.ws.readyState !== WebSocket.OPEN) return;
        const msgId = parseInt(btn.dataset.msgid);
        const code = decodeURIComponent(escape(atob(btn.dataset.code)));
        const lang = btn.dataset.lang;
        window.ws.send(JSON.stringify({ type: 'execute', message_id: msgId, code: code, language: lang }));
        btn.textContent = 'Running...';
        btn.disabled = true;
        setTimeout(function() { btn.textContent = 'Run'; btn.disabled = false; }, 35000);
    };

    window._requestExecution = function(btn) {
        if (!window.ws || window.ws.readyState !== WebSocket.OPEN) return;
        const msgId = btn.dataset.msgid;
        const lang = btn.dataset.lang;
        window.ws.send(JSON.stringify({ sender: 'Jordan', message: 'Could someone run the ' + lang + ' code block in message #' + msgId + '?' }));
    };

    // --- Image Modal ---
    window._showImageModal = function(url) {
        if (modalImg) modalImg.src = url;
        if (imageModal) imageModal.classList.add('active');
    };

    // --- File Upload ---
    function uploadFile(file) {
        if (!file) return;
        if (file.size > MAX_FILE_SIZE) { alert('File too large. Maximum: 50MB.'); return; }
        const formData = new FormData();
        formData.append('file', file);
        formData.append('sender', 'Jordan');
        if (uploadProgress) uploadProgress.style.display = 'block';
        if (progressFill) progressFill.style.width = '0%';
        if (progressText) progressText.textContent = 'Uploading ' + file.name + '...';
        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/api/upload');
        xhr.upload.onprogress = function(e) {
            if (e.lengthComputable) {
                const pct = Math.round((e.loaded / e.total) * 100);
                if (progressFill) progressFill.style.width = pct + '%';
                if (progressText) progressText.textContent = 'Uploading ' + file.name + '... ' + pct + '%';
            }
        };
        xhr.onload = function() {
            if (uploadProgress) uploadProgress.style.display = 'none';
            if (xhr.status !== 200) {
                try { alert('Upload failed: ' + JSON.parse(xhr.responseText).error); }
                catch(e) { alert('Upload failed: ' + xhr.statusText); }
            }
        };
        xhr.onerror = function() { if (uploadProgress) uploadProgress.style.display = 'none'; alert('Upload failed: network error'); };
        xhr.send(formData);
    }

    // --- Event Listeners ---
    function bindEvents() {
        // Attach button
        if (attachBtn) {
            attachBtn.addEventListener('click', function() { fileInput.click(); });
        }
        if (fileInput) {
            fileInput.addEventListener('change', function() {
                if (fileInput.files.length > 0) { uploadFile(fileInput.files[0]); fileInput.value = ''; }
            });
        }

        // Image modal close
        if (imageModal) {
            imageModal.addEventListener('click', function() {
                imageModal.classList.remove('active');
                if (modalImg) modalImg.src = '';
            });
        }

        // Drag-and-drop
        let dragCounter = 0;
        document.addEventListener('dragenter', function(e) { e.preventDefault(); dragCounter++; if (dropOverlay) dropOverlay.classList.add('active'); });
        document.addEventListener('dragleave', function(e) { e.preventDefault(); dragCounter--; if (dragCounter <= 0) { dragCounter = 0; if (dropOverlay) dropOverlay.classList.remove('active'); } });
        document.addEventListener('dragover', function(e) { e.preventDefault(); });
        document.addEventListener('drop', function(e) { e.preventDefault(); dragCounter = 0; if (dropOverlay) dropOverlay.classList.remove('active'); if (e.dataTransfer.files.length > 0) uploadFile(e.dataTransfer.files[0]); });

        // Paste image support (Ctrl+V)
        document.addEventListener('paste', function(e) {
            const items = e.clipboardData && e.clipboardData.items;
            if (!items) return;
            for (let i = 0; i < items.length; i++) {
                if (items[i].type.startsWith('image/')) {
                    e.preventDefault();
                    const blob = items[i].getAsFile();
                    if (blob) {
                        const ext = blob.type.split('/')[1] || 'png';
                        uploadFile(new File([blob], 'pasted-image.' + ext, { type: blob.type }));
                    }
                    return;
                }
            }
        });
    }

    // --- Monkey-patch appendMessage to handle file messages ---
    function patchAppendMessage() {
        const _origAppendMessage = window.appendMessage;
        if (!_origAppendMessage) return;

        window.appendMessage = function(msg) {
            // If it's a file message, render with file content
            if (msg.file_url) {
                const messagesEl = document.getElementById('messages');
                if (!messagesEl) return _origAppendMessage(msg);

                if (window.trackSender) window.trackSender(msg.sender);
                const div = document.createElement('div');
                let classes = 'msg ' + (window.getMsgClass ? window.getMsgClass(msg.sender_type) : 'human');
                if (msg.pinned) classes += ' pinned';
                div.className = classes;
                div.dataset.msgId = msg.id || '';

                const avatarClass = window.getAvatarClass ? window.getAvatarClass(msg.sender) : 'avatar-jordan';
                const senderClass = window.getSenderClass ? window.getSenderClass(msg.sender) : 'sender-jordan';
                const initial = window.getInitial ? window.getInitial(msg.sender) : msg.sender[0];
                const time = window.formatTime ? window.formatTime(msg.timestamp) : '';

                div.innerHTML = '<div class="msg-avatar ' + avatarClass + '">' + initial + '</div>' +
                    '<div class="msg-body">' +
                    '<div class="msg-header"><span class="msg-sender ' + senderClass + '">' + escapeHtmlLocal(msg.sender) + '</span><span class="msg-time">' + time + '</span></div>' +
                    window.renderFileContent(msg) +
                    '</div>';
                messagesEl.appendChild(div);
                messagesEl.scrollTop = messagesEl.scrollHeight;
            } else {
                // Use original but pass extra args for code block rendering
                _origAppendMessage(msg);
            }
        };
    }

    // --- Initialize ---
    function init() {
        injectStyles();
        ensureElements();
        bindEvents();
        patchAppendMessage();
        console.log('[chatroom_features] File upload & code execution loaded');
    }

    // Wait for DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        // Small delay to ensure the main chatroom JS has loaded
        setTimeout(init, 100);
    }
})();
