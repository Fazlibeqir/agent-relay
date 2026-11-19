const RelayDialog = (() => {
  const dialog = document.createElement("dialog");
  dialog.className = "relay-dialog";
  dialog.setAttribute("aria-labelledby", "relay-dialog-title");
  dialog.setAttribute("aria-describedby", "relay-dialog-message");
  dialog.innerHTML = `<form method="dialog">
    <h2 id="relay-dialog-title"></h2>
    <p id="relay-dialog-message"></p>
    <label for="relay-dialog-field" hidden></label>
    <textarea id="relay-dialog-field" rows="4" hidden></textarea>
    <div class="relay-dialog-actions">
      <button type="button" data-cancel>Cancel</button>
      <button type="submit" value="accept" data-accept></button>
    </div>
  </form>`;
  document.body.append(dialog);
  const title = dialog.querySelector("h2");
  const message = dialog.querySelector("p");
  const label = dialog.querySelector("label");
  const field = dialog.querySelector("textarea");
  const cancel = dialog.querySelector("[data-cancel]");
  const accept = dialog.querySelector("[data-accept]");
  cancel.onclick = () => dialog.close("");
  dialog.querySelector("form").onsubmit = event => {
    if (!field.hidden && !field.value.trim()) {
      event.preventDefault();
      field.focus();
    }
  };

  function ask({ heading, detail = "", action, fieldLabel, value = "", trigger = document.activeElement }) {
    if (dialog.open) return Promise.resolve(null);
    title.textContent = heading;
    message.textContent = detail;
    message.hidden = !detail;
    label.hidden = field.hidden = !fieldLabel;
    label.textContent = fieldLabel || "";
    field.value = value;
    accept.textContent = action;
    dialog.returnValue = "";
    return new Promise(resolve => {
      dialog.addEventListener("close", () => {
        const answer = dialog.returnValue === "accept" ? (fieldLabel ? field.value.trim() : true) : null;
        if (trigger instanceof HTMLElement && trigger.isConnected) trigger.focus({ preventScroll: true });
        resolve(answer);
      }, { once: true });
      dialog.showModal();
      (fieldLabel ? field : cancel).focus();
    });
  }

  const notice = document.createElement("aside");
  notice.className = "relay-notice";
  notice.hidden = true;
  notice.innerHTML = '<span role="status" aria-atomic="true"></span><button type="button" aria-label="Dismiss message">Dismiss</button>';
  document.body.append(notice);
  const status = notice.querySelector("span");
  notice.querySelector("button").onclick = () => { notice.hidden = true; status.textContent = ""; };
  function show(text, error = false) {
    notice.dataset.error = String(error);
    notice.hidden = false;
    status.textContent = text || (error ? "Relay couldn’t finish that action." : "");
  }

  return { ask, show };
})();
