// Turns every <textarea class="wysiwyg"> on the article admin page into a
// TinyMCE editor styled like the public article page. Inserted images are
// uploaded one at a time, untouched, to ArticleAdmin.upload_image_view, which
// stores a screen-sized copy for the article plus a full-resolution copy that
// the photo links to once the article is saved.

function csrfToken() {
  var input = document.querySelector("input[name=csrfmiddlewaretoken]");
  return input ? input.value : "";
}

// Uploads an image file/blob; resolves to the URL to use as its src.
function uploadImage(uploadUrl, blob, filename) {
  var form = new FormData();
  form.append("file", blob, filename || "image");
  return fetch(uploadUrl, {
    method: "POST",
    body: form,
    headers: { "X-CSRFToken": csrfToken() },
    credentials: "same-origin",
  }).then(function (response) {
    return response.json().catch(function () { return {}; }).then(function (data) {
      if (!response.ok || !data.location) {
        throw new Error(data.error || "Image upload failed (" + response.status + ").");
      }
      return data.location;
    });
  });
}

document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("textarea.wysiwyg").forEach(function (textarea) {
    tinymce.init({
      target: textarea,
      license_key: "gpl",
      promotion: false,
      branding: false,
      menubar: false,
      height: parseInt(textarea.dataset.height || "600", 10),
      plugins: "lists link image autolink code wordcount",
      toolbar:
        "undo redo | blocks fontsize | bold italic underline superscript | " +
        "alignleft aligncenter alignright alignjustify | bullist numlist outdent indent | " +
        "link image | removeformat code",
      font_size_formats: "12px 14px 16px 18px 20px 24px 28px 36px 48px",
      // Keep /media/... image paths exactly as written instead of rewriting them.
      convert_urls: false,
      paste_data_images: true,
      image_title: true,
      // Pasted and dropped images (and the image dialog's Upload tab).
      images_upload_handler: function (blobInfo) {
        return uploadImage(textarea.dataset.uploadUrl, blobInfo.blob(), blobInfo.filename());
      },
      file_picker_types: "image",
      file_picker_callback: function (callback) {
        var input = document.createElement("input");
        input.type = "file";
        input.accept = "image/*";
        input.onchange = function () {
          var file = input.files[0];
          uploadImage(textarea.dataset.uploadUrl, file, file.name).then(function (url) {
            callback(url, { alt: file.name.replace(/\.[^.]+$/, "") });
          }).catch(function (err) {
            alert(err.message);
          });
        };
        input.click();
      },
      // Match the public article page (base.html .article-content).
      content_style:
        'body { font-family: "Times New Roman", Times, serif; font-size: 20px; ' +
        "line-height: 1.6; max-width: 800px; margin: 1rem auto; padding: 0 1rem; } " +
        "img { max-width: 100%; height: auto; }",
      setup: function (editor) {
        editor.on("change input undo redo", function () { editor.save(); });
      },
    });
  });
});
