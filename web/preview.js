/* Document content stays inside an isolated frame, separate from Workbench. */
(() => {
  'use strict';
  let dialog, frame, message, download, request = 0, objectUrl = null;
  function dispose() {
    ++request;
    if (frame) frame.remove();
    frame = null;
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  }
  function setup() {
    if (dialog) return;
    dialog = document.createElement('dialog');
    dialog.setAttribute('aria-labelledby', 'documentPreviewTitle');
    dialog.style.cssText = 'width:min(1200px,calc(100vw - 32px));max-height:95vh;padding:0';
    dialog.innerHTML = '<div class="dialog-head"><div><h2 id="documentPreviewTitle"></h2><p>Document preview</p></div><button type="button" class="btn" data-preview-close aria-label="Close document preview">Close</button></div><div style="padding:12px 24px;display:flex;gap:16px;align-items:center"><span data-preview-message role="status" style="flex:1"></span><button type="button" class="btn" data-preview-download>Download</button></div>';
    document.body.appendChild(dialog);
    message = dialog.querySelector('[data-preview-message]');
    download = dialog.querySelector('[data-preview-download]');
    dialog.querySelector('[data-preview-close]').onclick = () => dialog.close();
    dialog.addEventListener('close', dispose);
  }
  function isolatedHtml(html) {
    const doc = new DOMParser().parseFromString(html, 'text/html');
    // Prevent redirects and keep report resources self-contained. The sandbox
    // separately disables scripts, forms, popups, and access to the parent app.
    doc.querySelectorAll('base,meta[http-equiv],script,iframe,object,embed').forEach(node => node.remove());
    const policy = doc.createElement('meta');
    policy.httpEquiv = 'Content-Security-Policy';
    policy.content = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; form-action 'none'; base-uri 'none'";
    doc.head.prepend(policy);
    return '<!doctype html>' + doc.documentElement.outerHTML;
  }
  window.WorkbenchPreview = {
    async open({name, url, token, onDownload}) {
      setup(); dispose();
      const current = request;
      dialog.querySelector('#documentPreviewTitle').textContent = name;
      message.textContent = 'Loading document…';
      download.onclick = async () => {
        try { await onDownload(); } catch (error) { message.textContent = error.message; }
      };
      if (!dialog.open) dialog.showModal();
      try {
        const response = await fetch(url, {headers:{'X-Workbench-Token':token},cache:'no-store'});
        if (!response.ok) throw new Error((await response.json()).error || 'Could not open this document.');
        const pdf = name.toLowerCase().endsWith('.pdf');
        const content = pdf ? await response.blob() : await response.json();
        if (current !== request) return;
        frame = document.createElement('iframe');
        frame.title = name;
        frame.style.cssText = 'display:block;width:100%;height:72vh;border:0;background:white';
        frame.referrerPolicy = 'no-referrer';
        if (pdf) {
          objectUrl = URL.createObjectURL(new Blob([content], {type:'application/pdf'}));
          frame.src = objectUrl;
          message.textContent = 'PDF preview. If your browser cannot display it, use Download.';
        } else {
          frame.setAttribute('sandbox', '');
          frame.srcdoc = isolatedHtml(content.html);
          message.textContent = name.toLowerCase().endsWith('.md') ? 'Formatted Markdown' : 'HTML report';
        }
        dialog.appendChild(frame);
      } catch(error) {
        if (current === request) message.textContent = 'Could not preview: '+error.message;
      }
    }
  };
})();
