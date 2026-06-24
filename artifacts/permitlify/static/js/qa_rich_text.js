/* Rich-text editor wiring for QA Team Tasks.
 *
 * Every `.rt-quill` element becomes a Quill (snow) editor. Its HTML is mirrored
 * into a hidden <textarea> (selector in data-target) so a plain form POST carries
 * the markup; the server re-sanitises it. Images upload to data-upload-url (CSRF
 * via data-csrf) and are inserted by URL, never base64 — whether picked from the
 * toolbar, pasted from the clipboard, or dropped onto the editor. */
(function () {
  if (typeof Quill === "undefined") return;

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
      if (quill.getText().trim() === "" && html.indexOf("<img") === -1) html = "";
      target.value = html;
    }
    quill.on("text-change", sync);
    sync();
    var form = el.closest("form");
    if (form) form.addEventListener("submit", sync);

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
