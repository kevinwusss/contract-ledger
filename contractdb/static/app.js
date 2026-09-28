"use strict";

const backToTop = document.getElementById("back-to-top");
if (backToTop) {
  let scrollUpdateQueued = false;
  function updateBackToTop() {
    backToTop.hidden = window.scrollY < 360;
    scrollUpdateQueued = false;
  }
  window.addEventListener("scroll", () => {
    if (scrollUpdateQueued) return;
    scrollUpdateQueued = true;
    requestAnimationFrame(updateBackToTop);
  }, { passive: true });
  backToTop.addEventListener("click", () => {
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    window.scrollTo({ top: 0, behavior: reducedMotion ? "auto" : "smooth" });
  });
  updateBackToTop();
}

document.querySelectorAll("[data-type-select]").forEach(typeSelect => {
  const subtypeSelect = typeSelect.closest("form").querySelector("[data-subtype-select]");
  if (!subtypeSelect) return;
  function update(reset) {
    const selected = typeSelect.value;
    [...subtypeSelect.options].forEach(option => {
      const mismatched = !!option.value && !!selected && option.dataset.type !== selected;
      option.hidden = mismatched;
      option.disabled = mismatched;
      if (reset && mismatched && option.selected) subtypeSelect.value = "";
    });
  }
  update(false);
  typeSelect.addEventListener("change", () => update(true));
});

const companyFilter = document.querySelector("[data-company-filter]");
const projectFilter = document.querySelector("[data-project-filter]");
if (companyFilter && projectFilter) {
  function updateProjects(reset) {
    [...projectFilter.options].forEach(option => {
      const hidden = !!option.value && !!companyFilter.value && option.dataset.company !== companyFilter.value;
      option.hidden = hidden;
      option.disabled = hidden;
      if (reset && hidden && option.selected) projectFilter.value = "";
    });
  }
  updateProjects(false);
  companyFilter.addEventListener("change", () => updateProjects(true));
}

document.querySelectorAll("[data-candidate-field]").forEach(button => {
  button.addEventListener("click", () => {
    const key = button.dataset.candidateField;
    const input = document.getElementById(key);
    if (!input || input.disabled) return;
    if (key === "subtype" && input.tagName === "SELECT") {
      const option = [...input.options].find(item => item.text === button.dataset.candidateValue && !item.disabled);
      if (!option) { alert("请先选择与该子类别对应的导入类型。"); return; }
      input.value = option.value;
    } else {
      input.value = button.dataset.candidateValue;
    }
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.classList.add("candidate-applied");
    setTimeout(() => input.classList.remove("candidate-applied"), 1300);
    button.textContent = "已填入";
    input.focus({ preventScroll: true });
  });
});

document.querySelectorAll("form[data-confirm]").forEach(form => {
  form.addEventListener("submit", event => { if (!confirm(form.dataset.confirm)) event.preventDefault(); });
});
document.querySelectorAll("form[data-loading]").forEach(form => {
  form.addEventListener("submit", () => {
    const button = form.querySelector("button[type=submit]");
    if (button) { button.disabled = true; button.textContent = "处理中…"; }
  });
});
document.querySelectorAll("[data-go-back]").forEach(button => button.addEventListener("click", () => history.back()));
const files = document.getElementById("files");
const recordKind = document.getElementById("record-kind");
if (recordKind) recordKind.addEventListener("change", () => {
  const invoice = recordKind.value === "invoice";
  document.getElementById("record-reference-label").textContent = invoice ? "发票号码（必填）" : "收付款凭证或流水号";
  document.getElementById("record-reference").required = invoice;
});
if (files) files.addEventListener("change", () => {
  const selected = [...files.files];
  const total = selected.reduce((sum, file) => sum + file.size, 0);
  document.getElementById("file-selection").textContent = selected.length ? `已选择 ${selected.length} 份 · 共 ${(total / 1024 / 1024).toFixed(1)} MB` : "";
  files.setCustomValidity(selected.length > 20 ? "一次最多选择 20 份文件" : total > 100 * 1024 * 1024 ? "文件总大小超过 100 MB，请分批上传" : "");
});
const extraction = document.querySelector("[data-extraction-status]");
if (extraction && ["queued", "processing"].includes(extraction.dataset.extractionStatus)) {
  const timer = setInterval(async () => {
    try {
      const response = await fetch(extraction.dataset.pollUrl, { headers: { Accept: "application/json" } });
      if (!response.ok || response.redirected) { clearInterval(timer); return; }
      const data = await response.json();
      if (["ready", "error"].includes(data.status)) {
        clearInterval(timer);
        const message = document.getElementById("extraction-message");
        message.textContent = data.status === "ready" ? "本地提取已完成。请先保存当前填写内容，再刷新查看候选信息。" : "本地提取未完成。请保存当前内容后刷新，查看原因或重试。";
        const reload = document.createElement("a");
        reload.href = location.pathname;
        reload.textContent = " 刷新页面";
        message.append(reload);
      }
    } catch (_) { /* Temporary network failure: retain the form and retry. */ }
  }, 5000);
}
document.querySelectorAll("[data-check-all]").forEach(box => {
  const form = box.closest("form");
  if (!form) return;
  const items = [...form.querySelectorAll('input[name="ids"]')];
  const sync = () => {
    const checked = items.filter(item => item.checked).length;
    box.checked = items.length > 0 && checked === items.length;
    box.indeterminate = checked > 0 && checked < items.length;
  };
  box.addEventListener("change", () => items.forEach(item => { item.checked = box.checked; }));
  items.forEach(item => item.addEventListener("change", sync));
});
