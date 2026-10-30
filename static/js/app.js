/* Janaushadhi Saathi progressive enhancements.
 * All text is inserted with textContent; no HTML is built from user input.
 */
(function () {
  "use strict";

  var i18n = {};
  try {
    var node = document.getElementById("app-i18n");
    if (node) i18n = JSON.parse(node.textContent || "{}");
  } catch (error) {
    i18n = {};
  }
  function tr(key) { return i18n[key] || key; }

  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  var csrfToken = csrfMeta ? csrfMeta.getAttribute("content") : "";

  var toastTimer = null;
  function toast(message) {
    var el = document.getElementById("toast");
    if (!el) return;
    el.textContent = message;
    el.hidden = false;
    if (toastTimer) window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () { el.hidden = true; }, 3600);
  }

  function formatINR(paise) {
    if (paise === null || paise === undefined || isNaN(paise)) return "—";
    try {
      return new Intl.NumberFormat("en-IN", {
        style: "currency", currency: "INR", minimumFractionDigits: 2
      }).format(paise / 100);
    } catch (error) {
      return "₹" + (paise / 100).toFixed(2);
    }
  }

  async function readJSON(response) {
    var data = null;
    try { data = await response.json(); } catch (error) { data = null; }
    if (!response.ok || !data || data.ok === false) {
      var message = data && data.error && data.error.message
        ? data.error.message
        : tr("Something went wrong");
      var failure = new Error(message);
      failure.payload = data;
      throw failure;
    }
    return data;
  }

  function el(tag, className, text) {
    var item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined && text !== null) item.textContent = String(text);
    return item;
  }

  /* ------------------------------------------------------------------ */
  /* Small utilities                                                     */
  /* ------------------------------------------------------------------ */

  document.querySelectorAll("[data-print]").forEach(function (button) {
    button.addEventListener("click", function () { window.print(); });
  });
  document.querySelectorAll("[data-retry]").forEach(function (button) {
    button.addEventListener("click", function () { window.location.reload(); });
  });

  document.querySelectorAll("[data-copy-target]").forEach(function (button) {
    button.addEventListener("click", function () {
      var source = document.getElementById(button.getAttribute("data-copy-target"));
      var text = source ? source.textContent.trim() : "";
      copyText(text);
    });
  });

  function copyText(text) {
    function fallback() {
      var area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.className = "sr-only";
      document.body.appendChild(area);
      area.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (error) { ok = false; }
      document.body.removeChild(area);
      toast(ok ? tr("Copied to clipboard") : tr("Copy failed"));
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        toast(tr("Copied to clipboard"));
      }).catch(fallback);
    } else {
      fallback();
    }
  }

  document.querySelectorAll("[data-share]").forEach(function (button) {
    button.addEventListener("click", function () {
      var source = document.getElementById("medicine-share-text");
      var text = source ? source.textContent.trim() : document.title;
      if (navigator.share) {
        navigator.share({ title: document.title, text: text, url: window.location.href })
          .catch(function () { /* user cancelled */ });
      } else {
        copyText(text);
      }
    });
  });

  /* ------------------------------------------------------------------ */
  /* Savings list (localStorage only)                                    */
  /* ------------------------------------------------------------------ */

  var STORAGE_KEY = "js_savings_v1";
  var savingsApp = document.getElementById("savings-app");

  function loadList() {
    try {
      var raw = window.localStorage.getItem(STORAGE_KEY);
      var parsed = raw ? JSON.parse(raw) : [];
      return Array.isArray(parsed) ? parsed : [];
    } catch (error) {
      return [];
    }
  }

  function saveList(items) {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
      return true;
    } catch (error) {
      toast(tr("Something went wrong"));
      return false;
    }
  }

  document.querySelectorAll("[data-add-comparison]").forEach(function (button) {
    button.addEventListener("click", function () {
      var data = button.dataset;
      var price = parseFloat(String(data.brandPrice || "").replace(/[^0-9.]/g, ""));
      if (!price || price <= 0) {
        toast(tr("Enter a valid branded price greater than zero."));
        return;
      }
      var packSize = parseFloat(data.brandPackSize || "");
      if (!packSize || packSize <= 0) {
        toast(tr("Pack size and quantity must be greater than zero."));
        return;
      }
      var items = loadList().filter(function (item) {
        return item.medicine_id !== data.medicineId;
      });
      items.push({
        medicine_id: data.medicineId,
        name: data.medicineName,
        brand_price: price.toFixed(2),
        brand_pack_size: packSize,
        quantity: parseInt(data.quantity, 10) || 1,
        purchases_per_month: parseInt(data.purchases, 10) || 1,
        unit: data.unit || ""
      });
      if (saveList(items)) toast(tr("Added to your comparison list"));
    });
  });

  if (savingsApp) {
    var emptyBox = document.getElementById("savings-empty");
    var container = document.getElementById("savings-container");
    var rowsBody = document.getElementById("savings-rows");
    var status = document.getElementById("savings-status");
    var recalcTimer = null;

    function setStatus(message) {
      if (status) status.textContent = message || "";
    }

    function render() {
      var items = loadList();
      if (!items.length) {
        if (emptyBox) emptyBox.hidden = false;
        if (container) container.hidden = true;
        setStatus("");
        return;
      }
      if (emptyBox) emptyBox.hidden = true;
      if (container) container.hidden = false;
      rowsBody.textContent = "";

      items.forEach(function (item, index) {
        var row = document.createElement("tr");

        var nameCell = document.createElement("td");
        nameCell.className = "med-name";
        nameCell.appendChild(document.createTextNode(item.name || item.medicine_id));
        var sub = el("small", null, item.unit ? tr("per") + " " + item.unit : "");
        if (sub.textContent) nameCell.appendChild(sub);
        row.appendChild(nameCell);

        row.appendChild(inputCell(item.brand_price, "number", "0.01", tr("Branded price you entered"), function (value) {
          item.brand_price = value;
          persist();
        }));
        row.appendChild(inputCell(item.quantity, "number", "1", tr("Quantity"), function (value) {
          item.quantity = value;
          persist();
        }));
        row.appendChild(inputCell(item.purchases_per_month, "number", "1", tr("Purchases per month"), function (value) {
          item.purchases_per_month = value;
          persist();
        }));

        var diffCell = el("td", "difference-cell", "—");
        diffCell.setAttribute("data-difference-index", String(index));
        row.appendChild(diffCell);

        var removeCell = document.createElement("td");
        var removeButton = el("button", "btn btn-quiet", tr("Remove"));
        removeButton.type = "button";
        removeButton.addEventListener("click", function () {
          var current = loadList();
          current.splice(index, 1);
          saveList(current);
          render();
        });
        removeCell.appendChild(removeButton);
        row.appendChild(removeCell);
        rowsBody.appendChild(row);
      });
      recalculate(items);
    }

    function inputCell(value, type, step, label, onChange) {
      var cell = document.createElement("td");
      var input = document.createElement("input");
      input.type = type;
      input.step = step;
      input.min = "0";
      input.value = value === undefined || value === null ? "" : value;
      input.setAttribute("aria-label", label || "");
      input.addEventListener("change", function () {
        onChange(input.value);
      });
      cell.appendChild(input);
      return cell;
    }

    function persist() {
      var items = loadList();
      // Values are read from the DOM order each time by render(); to keep this
      // simple we rebuild state from the current inputs before saving.
      var inputs = rowsBody ? rowsBody.querySelectorAll("input") : [];
      for (var i = 0; i < items.length; i += 1) {
        var offset = i * 3;
        if (inputs[offset]) items[i].brand_price = inputs[offset].value;
        if (inputs[offset + 1]) items[i].quantity = inputs[offset + 1].value;
        if (inputs[offset + 2]) items[i].purchases_per_month = inputs[offset + 2].value;
      }
      saveList(items);
      if (recalcTimer) window.clearTimeout(recalcTimer);
      recalcTimer = window.setTimeout(function () { recalculate(items); }, 350);
    }

    async function recalculate(items) {
      if (!items.length) return;
      setStatus(tr("Calculating…"));
      var payload = {
        items: items.map(function (item) {
          return {
            medicine_id: item.medicine_id,
            brand_price: item.brand_price,
            brand_pack_size: item.brand_pack_size,
            quantity: item.quantity,
            purchases_per_month: item.purchases_per_month
          };
        })
      };
      try {
        var response = await fetch("/api/savings", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload)
        });
        var data = await readJSON(response);
        setStatus("");
        applyTotals(data.totals);
        (data.items || []).forEach(function (entry) {
          var cell = rowsBody.querySelector('[data-difference-index="' + entry.index + '"]');
          if (!cell) return;
          if (entry.ok && entry.comparison) {
            cell.textContent = formatINR(entry.comparison.difference_paise) +
              " (" + entry.comparison.difference_percent + "%)";
          } else {
            cell.textContent = "—";
          }
        });
      } catch (error) {
        setStatus(error.message || tr("One or more entries could not be calculated. Check the prices and pack sizes."));
      }
    }

    function applyTotals(totals) {
      var brand = savingsApp.querySelector('[data-total="brand"]');
      var generic = savingsApp.querySelector('[data-total="generic"]');
      var difference = savingsApp.querySelector('[data-total="difference"]');
      var yearly = savingsApp.querySelector('[data-total="yearly"]');
      if (brand) brand.textContent = formatINR(totals.brand_total_paise);
      if (generic) generic.textContent = formatINR(totals.catalogue_total_paise);
      if (difference) difference.textContent = formatINR(totals.difference_paise) +
        " (" + totals.difference_percent + "%)";
      if (yearly) yearly.textContent = formatINR(totals.yearly_difference_paise);
    }

    var clearButton = document.querySelector("[data-savings-clear]");
    if (clearButton) {
      clearButton.addEventListener("click", function () {
        saveList([]);
        render();
        toast(tr("Clear my list"));
      });
    }
    var printButton = document.querySelector("[data-savings-print]");
    if (printButton) {
      printButton.addEventListener("click", function () { window.print(); });
    }

    render();
  }

  /* ------------------------------------------------------------------ */
  /* OCR                                                                 */
  /* ------------------------------------------------------------------ */

  var ocrForm = document.getElementById("ocr-form");
  var fileInput = document.getElementById("ocr-file");
  var ocrText = document.getElementById("ocr-text");
  var ocrStatus = document.getElementById("ocr-status");
  var ocrResults = document.getElementById("ocr-results");
  var ocrCandidates = document.getElementById("ocr-candidates");
  var previewBox = document.getElementById("ocr-preview");
  var previewImage = document.getElementById("ocr-preview-image");
  var previewURL = null;

  function ocrMessage(message) {
    if (ocrStatus) ocrStatus.textContent = message || "";
  }

  if (fileInput) {
    fileInput.addEventListener("change", function () {
      if (previewURL) { URL.revokeObjectURL(previewURL); previewURL = null; }
      var file = fileInput.files && fileInput.files[0];
      if (!file) { if (previewBox) previewBox.hidden = true; return; }
      if (file.size > 8 * 1024 * 1024) {
        ocrMessage(tr("The file is too large. Please use an image under 8 MB."));
        fileInput.value = "";
        return;
      }
      if (file.type && !/^image\/(jpeg|png|webp|bmp|tiff)$/.test(file.type)) {
        ocrMessage(tr("Unsupported file type. Use JPEG, PNG, WebP, BMP or TIFF."));
        fileInput.value = "";
        return;
      }
      previewURL = URL.createObjectURL(file);
      if (previewImage) previewImage.src = previewURL;
      if (previewBox) previewBox.hidden = false;
    });
  }

  if (ocrForm) {
    ocrForm.addEventListener("submit", async function (event) {
      event.preventDefault();
      var file = fileInput && fileInput.files && fileInput.files[0];
      if (!file) { ocrMessage(tr("The image could not be read.")); return; }
      var submitButton = ocrForm.querySelector("[data-ocr-upload]");
      if (submitButton) submitButton.disabled = true;
      ocrMessage(tr("Extracting…"));
      var body = new FormData();
      body.append("image", file);
      try {
        var response = await fetch("/api/ocr", { method: "POST", body: body });
        var data = await readJSON(response);
        if (ocrText) ocrText.value = data.text || "";
        if (data.text) {
          ocrMessage("");
          ocrText.focus();
        } else {
          ocrMessage(tr("The image could not be read."));
        }
      } catch (error) {
        ocrMessage(error.message || tr("The image could not be read."));
      } finally {
        if (submitButton) submitButton.disabled = false;
      }
    });
  }

  var analyzeButton = document.querySelector("[data-ocr-analyze]");
  if (analyzeButton) {
    analyzeButton.addEventListener("click", async function () {
      var text = ocrText ? ocrText.value.trim() : "";
      if (!text) { ocrMessage(tr("Enter or extract some text to analyse.")); return; }
      analyzeButton.disabled = true;
      ocrMessage(tr("Analysing…"));
      try {
        var response = await fetch("/api/analyze", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: text })
        });
        var data = await readJSON(response);
        ocrMessage("");
        renderCandidates(data.candidates || []);
      } catch (error) {
        ocrMessage(error.message || tr("Something went wrong"));
      } finally {
        analyzeButton.disabled = false;
      }
    });
  }

  function renderCandidates(candidates) {
    if (!ocrCandidates || !ocrResults) return;
    ocrCandidates.textContent = "";
    if (!candidates.length) {
      var none = el("li", "empty-inline", tr("No confident catalogue match was found. Check the spelling or search manually."));
      ocrCandidates.appendChild(none);
      ocrResults.hidden = false;
      return;
    }
    candidates.forEach(function (candidate) {
      var medicine = candidate.medicine || {};
      var item = el("li", "match-card");
      var main = document.createElement("div");
      var heading = document.createElement("h3");
      var link = document.createElement("a");
      link.href = "/medicine/" + encodeURIComponent(medicine.id || "");
      link.textContent = (medicine.generic_name || "") + " " + (medicine.strength || "");
      heading.appendChild(link);
      main.appendChild(heading);
      main.appendChild(el("p", "small", (medicine.composition || "") + " · " +
        (medicine.dosage_form || "") + " · " + (medicine.pack_size || "") + " " +
        (medicine.pack_unit || "")));
      if (candidate.confidence) {
        main.appendChild(el("p", "small muted", tr("Confidence") + ": " + Math.round(candidate.confidence * 100) + "%"));
      }
      if (candidate.reasons && candidate.reasons.length) {
        var list = el("ul", "reason-list");
        candidate.reasons.slice(0, 4).forEach(function (reason) {
          list.appendChild(el("li", null, reason.label || reason.code));
        });
        main.appendChild(list);
      }
      var side = el("div", "match-side");
      side.appendChild(el("span", "badge badge-info", tr("Uncertain")));
      side.appendChild(el("p", "small", tr("Medicine names, strengths and forms are only suggestions. A pharmacist must verify them.")));
      item.appendChild(main);
      item.appendChild(side);
      ocrCandidates.appendChild(item);
    });
    ocrResults.hidden = false;
    ocrResults.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  /* ------------------------------------------------------------------ */
  /* Nearby Kendra                                                       */
  /* ------------------------------------------------------------------ */

  var nearbyButton = document.querySelector("[data-nearby-kendra]");
  var nearbySection = document.getElementById("nearby-section");
  var nearbyResults = document.getElementById("nearby-results");
  var nearbyStatus = document.getElementById("nearby-status");

  if (nearbyButton) {
    nearbyButton.addEventListener("click", function () {
      if (!navigator.geolocation) {
        if (nearbyStatus) nearbyStatus.textContent = tr("Location unavailable");
        return;
      }
      if (nearbyStatus) nearbyStatus.textContent = tr("Locating…");
      navigator.geolocation.getCurrentPosition(function (position) {
        var lat = position.coords.latitude.toFixed(5);
        var lon = position.coords.longitude.toFixed(5);
        fetch("/api/kendras/nearby?lat=" + encodeURIComponent(lat) + "&lon=" + encodeURIComponent(lon))
          .then(readJSON)
          .then(function (data) {
            if (nearbySection) nearbySection.hidden = false;
            if (nearbyStatus) nearbyStatus.textContent = data.notice || "";
            renderNearby(data.data || []);
          })
          .catch(function (error) {
            if (nearbyStatus) nearbyStatus.textContent = error.message || tr("Location unavailable");
          });
      }, function () {
        if (nearbyStatus) nearbyStatus.textContent = tr("Location permission was denied. You can search manually instead.");
      }, { enableHighAccuracy: false, timeout: 10000, maximumAge: 300000 });
    });
  }

  function renderNearby(items) {
    if (!nearbyResults) return;
    nearbyResults.textContent = "";
    if (!items.length) {
      nearbyResults.appendChild(el("li", "empty-inline", tr("No Kendra matched these filters.")));
      return;
    }
    items.forEach(function (kendra) {
      var item = el("li", "result-card kendra-card");
      var main = document.createElement("div");
      main.className = "result-main";
      var title = el("div", "result-title");
      title.appendChild(el("h2", null, kendra.name));
      title.appendChild(el("span", "badge badge-warn", tr("Sample data")));
      main.appendChild(title);
      main.appendChild(el("p", "small", kendra.kendra_code));
      main.appendChild(el("p", null, kendra.address));
      main.appendChild(el("p", "muted", [kendra.city, kendra.district, kendra.state, kendra.pincode].filter(Boolean).join(", ")));
      var side = el("div", "result-side");
      if (typeof kendra.distance_km === "number") {
        side.appendChild(el("p", "price-display", kendra.distance_km + " km"));
      }
      if (kendra.latitude !== null && kendra.longitude !== null) {
        var directions = el("a", "btn btn-secondary", tr("Open directions") + " ↗");
        directions.href = "https://www.google.com/maps/dir/?api=1&destination=" +
          encodeURIComponent(kendra.latitude) + "," + encodeURIComponent(kendra.longitude);
        directions.target = "_blank";
        directions.rel = "noopener noreferrer";
        side.appendChild(directions);
      }
      item.appendChild(main);
      item.appendChild(side);
      nearbyResults.appendChild(item);
    });
  }

  /* ------------------------------------------------------------------ */
  /* Service worker                                                      */
  /* ------------------------------------------------------------------ */

  if ("serviceWorker" in navigator && (window.location.protocol === "https:" ||
      window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1")) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function () {
        /* Offline support is optional. */
      });
    });
  }
})();
