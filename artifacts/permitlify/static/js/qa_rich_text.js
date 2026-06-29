/* Rich-text editor wiring for QA Team Tasks.
 *
 * Every `.rt-quill` element becomes a Quill (snow) editor. Its HTML is mirrored
 * into a hidden <textarea> (selector in data-target) so a plain form POST carries
 * the markup; the server re-sanitises it. Images upload to data-upload-url (CSRF
 * via data-csrf) and are inserted by URL, never base64 — whether picked from the
 * toolbar, pasted from the clipboard, or dropped onto the editor.
 *
 * Typing "@" opens a member autocomplete (members fetched from data-mention-url);
 * choosing one inserts a non-editable mention token
 * (<span class="rt-mention" data-username="…">@name</span>) that the server reads
 * to notify the mentioned user. */
(function () {
  if (typeof Quill === "undefined") return;

  // --- Mention token: a non-editable inline embed carrying the username. ---
  (function registerMentionBlot() {
    if (window.__rtMentionRegistered) return;
    var Embed = Quill.import("blots/embed");
    class MentionBlot extends Embed {
      static create(value) {
        var node = super.create(value);
        node.setAttribute("data-username", value.username);
        node.textContent = "@" + (value.label || value.username);
        return node;
      }
      static value(node) {
        return {
          username: node.getAttribute("data-username") || "",
          label: (node.textContent || "").replace(/[\uFEFF\u200B]/g, "").replace(/^@/, ""),
        };
      }
    }
    MentionBlot.blotName = "mention";
    MentionBlot.tagName = "span";
    MentionBlot.className = "rt-mention";
    Quill.register(MentionBlot);
    window.__rtMentionRegistered = true;
  })();

  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : s;
    return d.innerHTML;
  }

  function setupMentions(quill) {
    var holder = quill.container.closest(".rt-quill") || quill.container;
    var url = holder.getAttribute("data-mention-url");
    if (!url) return;

    var usersPromise = null;
    function loadUsers() {
      if (!usersPromise) {
        usersPromise = fetch(url, {
          headers: { "X-Requested-With": "fetch" },
          credentials: "same-origin",
        })
          .then(function (r) {
            return r.ok ? r.json() : { users: [] };
          })
          .then(function (d) {
            return (d && d.users) || [];
          })
          .catch(function () {
            return [];
          });
      }
      return usersPromise;
    }

    var box = document.createElement("div");
    box.className = "rt-mention-list";
    box.setAttribute("hidden", "");
    document.body.appendChild(box);

    var state = { open: false, anchor: -1, query: "", items: [], active: 0 };

    function close() {
      state.open = false;
      box.setAttribute("hidden", "");
      box.innerHTML = "";
    }

    function position() {
      var bounds = quill.getBounds(state.anchor);
      if (!bounds) return;
      var rect = quill.root.getBoundingClientRect();
      box.style.left = window.scrollX + rect.left + bounds.left + "px";
      box.style.top = window.scrollY + rect.top + bounds.bottom + 4 + "px";
    }

    function render() {
      if (!state.items.length) {
        close();
        return;
      }
      box.innerHTML = state.items
        .map(function (u, i) {
          return (
            '<button type="button" class="rt-mention-opt' +
            (i === state.active ? " is-active" : "") +
            '" data-i="' +
            i +
            '">' +
            esc(u.label) +
            "</button>"
          );
        })
        .join("");
      box.removeAttribute("hidden");
      position();
    }

    function pick(u) {
      if (!u) return;
      var delLen = state.query.length + 1; // the '@' plus the typed query
      quill.deleteText(state.anchor, delLen, "user");
      quill.insertEmbed(
        state.anchor,
        "mention",
        { username: u.username, label: u.username },
        "user"
      );
      quill.insertText(state.anchor + 1, " ", "user");
      quill.setSelection(state.anchor + 2, "silent");
      close();
    }

    function update() {
      var sel = quill.getSelection();
      if (!sel || sel.length) {
        close();
        return;
      }
      var upto = quill.getText(0, sel.index);
      var m = /(?:^|\s)@([\w.\-]*)$/.exec(upto);
      if (!m) {
        close();
        return;
      }
      state.anchor = sel.index - m[1].length - 1; // index of the '@'
      state.query = m[1];
      loadUsers().then(function (users) {
        var q = state.query.toLowerCase();
        state.items = users
          .filter(function (u) {
            return (
              !q ||
              u.username.toLowerCase().indexOf(q) !== -1 ||
              (u.label || "").toLowerCase().indexOf(q) !== -1
            );
          })
          .slice(0, 8);
        state.active = 0;
        state.open = true;
        render();
      });
    }

    quill.on("text-change", function (delta, old, source) {
      if (source === "user") update();
    });
    quill.on("selection-change", function (range) {
      if (!range) close();
    });

    box.addEventListener("mousedown", function (e) {
      var opt = e.target.closest(".rt-mention-opt");
      if (!opt) return;
      e.preventDefault();
      pick(state.items[parseInt(opt.getAttribute("data-i"), 10)]);
    });

    quill.root.addEventListener(
      "keydown",
      function (e) {
        if (!state.open || !state.items.length) return;
        if (e.key === "ArrowDown") {
          e.preventDefault();
          e.stopPropagation();
          state.active = (state.active + 1) % state.items.length;
          render();
        } else if (e.key === "ArrowUp") {
          e.preventDefault();
          e.stopPropagation();
          state.active = (state.active - 1 + state.items.length) % state.items.length;
          render();
        } else if (e.key === "Enter" || e.key === "Tab") {
          e.preventDefault();
          e.stopPropagation();
          pick(state.items[state.active]);
        } else if (e.key === "Escape") {
          close();
        }
      },
      true
    );

    window.addEventListener("scroll", function () {
      if (state.open) position();
    }, true);
  }

  var TOOLBAR = [
    [{ header: [1, 2, 3, false] }],
    ["bold", "italic", "underline", "strike"],
    [{ color: [] }, { background: [] }],
    [{ list: "ordered" }, { list: "bullet" }],
    [{ align: [] }],
    ["blockquote", "code-block"],
    ["link", "image"],
    ["clean"],
  ];

  function initEditor(el) {
    var target = document.querySelector(el.getAttribute("data-target") || "");
    var uploadUrl = el.getAttribute("data-upload-url");
    var csrf = el.getAttribute("data-csrf");

    var quill = new Quill(el, {
      theme: "snow",
      placeholder: el.getAttribute("data-placeholder") || "",
      modules: { toolbar: TOOLBAR },
    });

    if (target && target.value) {
      quill.clipboard.dangerouslyPasteHTML(target.value);
    }

    function sync() {
      if (!target) return;
      var html = quill.root.innerHTML;
      if (quill.getText().trim() === "" && html.indexOf("<img") === -1 && html.indexOf("rt-mention") === -1) html = "";
      target.value = html;
    }
    quill.on("text-change", sync);
    sync();
    var form = el.closest("form");
    if (form) form.addEventListener("submit", sync);

    setupMentions(quill);

    function uploadAndInsert(file) {
      if (!file || !uploadUrl) return;
      var fd = new FormData();
      fd.append("image", file);
      fetch(uploadUrl, {
        method: "POST",
        headers: { "X-CSRFToken": csrf, "X-Requested-With": "fetch" },
        body: fd,
        credentials: "same-origin",
      })
        .then(function (r) {
          return r.json().then(function (j) {
            return { ok: r.ok, body: j };
          });
        })
        .then(function (res) {
          if (!res.ok) {
            window.alert((res.body && res.body.error) || "Upload failed.");
            return;
          }
          var range = quill.getSelection(true);
          var index = range ? range.index : quill.getLength();
          quill.insertEmbed(index, "image", res.body.url, "user");
          // Drop the cursor onto a fresh line below the image so typing
          // continues there instead of riding alongside the image.
          quill.insertText(index + 1, "\n", "user");
          quill.setSelection(index + 2, "silent");
          sync();
        })
        .catch(function () {
          window.alert("Upload failed.");
        });
    }

    function imageFilesFrom(list) {
      var out = [];
      for (var i = 0; i < (list || []).length; i++) {
        var item = list[i];
        var type = item.type || "";
        if (type.indexOf("image/") === 0) {
          var file = item.getAsFile ? item.getAsFile() : item;
          if (file) out.push(file);
        }
      }
      return out;
    }

    quill.getModule("toolbar").addHandler("image", function () {
      var input = document.createElement("input");
      input.type = "file";
      input.accept = "image/png,image/jpeg,image/gif,image/webp";
      input.onchange = function () {
        uploadAndInsert(input.files && input.files[0]);
      };
      input.click();
    });

    // Pasted screenshots: upload instead of letting Quill embed a base64 data
    // URL (which the server sanitiser would strip). Capture phase so we run
    // before Quill's own clipboard handler.
    quill.root.addEventListener(
      "paste",
      function (e) {
        var imgs = imageFilesFrom(e.clipboardData && e.clipboardData.items);
        if (!imgs.length) return;
        e.preventDefault();
        e.stopPropagation();
        imgs.forEach(uploadAndInsert);
      },
      true
    );

    // Dragged-in image files: upload them too.
    quill.root.addEventListener(
      "drop",
      function (e) {
        var imgs = imageFilesFrom(e.dataTransfer && e.dataTransfer.files);
        if (!imgs.length) return;
        e.preventDefault();
        e.stopPropagation();
        imgs.forEach(uploadAndInsert);
      },
      true
    );
  }

  document.querySelectorAll(".rt-quill").forEach(initEditor);
})();
