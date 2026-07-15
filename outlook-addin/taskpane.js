/* global Office */
"use strict";

let handoff = null;

Office.onReady((info) => {
  if (info.host !== Office.HostType.Outlook) {
    setStatus("This companion runs only in Outlook.");
    return;
  }
  refreshSelection();
  document.getElementById("export").addEventListener("click", exportMetadata);
});

function refreshSelection() {
  const item = Office.context.mailbox.item;
  if (!item) {
    setStatus("Select a message or appointment.");
    return;
  }
  const sender = item.from ?? item.organizer ?? {};
  const attachments = (item.attachments ?? []).slice(0, 250).map((value) => ({
    id: String(value.id ?? ""),
    name: String(value.name ?? "").slice(0, 512),
    mime_type: String(value.contentType ?? "").slice(0, 255),
    size: Number.isSafeInteger(value.size) && value.size >= 0 ? value.size : 0,
    is_inline: Boolean(value.isInline),
  })).filter((value) => value.id);
  handoff = {
    schema: "aiorganizer.outlook-selection/v1",
    exportedAt: new Date().toISOString(),
    source: "office-js-outlook-taskpane",
    item: {
      item_id: String(item.itemId ?? ""),
      item_type: item.itemType === Office.MailboxEnums.ItemType.Appointment ? "appointment" : "message",
      subject: String(item.subject ?? "").slice(0, 1000),
      sender: { name: String(sender.displayName ?? "").slice(0, 320), address: String(sender.emailAddress ?? "").slice(0, 320) },
      received_at: item.dateTimeCreated instanceof Date ? item.dateTimeCreated.toISOString() : "",
      attachments,
    },
  };
  renderSummary(handoff.item);
  document.getElementById("export").disabled = !handoff.item.item_id;
  setStatus(handoff.item.item_id ? "Metadata is ready for export." : "Outlook did not provide an item identifier.");
}

function renderSummary(item) {
  const summary = document.getElementById("summary");
  summary.replaceChildren();
  for (const [label, value] of [["Subject", item.subject], ["Sender", item.sender.address], ["Attachments", item.attachments.length]]) {
    const term = document.createElement("dt");
    const description = document.createElement("dd");
    term.textContent = label;
    description.textContent = String(value);
    summary.append(term, description);
  }
}

function exportMetadata() {
  if (!handoff) return;
  const blob = new Blob([JSON.stringify(handoff, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `AIOrganizer-Outlook-${new Date().toISOString().replaceAll(":", "-")}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
  setStatus("Metadata exported. Import it from AIOrganizer’s File menu.");
}

function setStatus(value) { document.getElementById("status").textContent = value; }
