// Turns every <textarea class="wysiwyg"> on the article admin page into a
// TinyMCE editor styled like the public article page. Pasted or inserted
// images are embedded as base64 here; ArticleAdmin.save_model converts them
// into compressed files under /media on save.
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
      file_picker_types: "image",
      file_picker_callback: function (callback) {
        var input = document.createElement("input");
        input.type = "file";
        input.accept = "image/*";
        input.onchange = function () {
          var file = input.files[0];
          var reader = new FileReader();
          reader.onload = function () { callback(reader.result, { alt: file.name }); };
          reader.readAsDataURL(file);
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
