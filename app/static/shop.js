"use strict";

(function () {
  const storageKey = "goodthings-demo-cart-v1";
  const cards = Array.from(document.querySelectorAll(".product-card"));
  const products = new Map(cards.map(function (card) {
    const image = card.querySelector(".product-image");
    return [card.dataset.id, {
      id: card.dataset.id,
      name: card.dataset.name,
      category: card.dataset.category,
      price: Number(card.dataset.price),
      image: image ? image.src : "",
    }];
  }));
  const elements = {
    cartCount: document.getElementById("cart-count"),
    drawerCount: document.getElementById("drawer-count"),
    lines: document.getElementById("cart-lines"),
    empty: document.getElementById("cart-empty"),
    checkout: document.getElementById("checkout-form"),
    total: document.getElementById("cart-total"),
    shippingAway: document.getElementById("shipping-away"),
    checkoutItems: document.getElementById("checkout-items"),
    drawer: document.getElementById("cart-drawer"),
    backdrop: document.getElementById("cart-backdrop"),
    search: document.getElementById("search"),
    sort: document.getElementById("sort"),
    grid: document.getElementById("product-grid"),
    resultCount: document.getElementById("result-count"),
    emptyResults: document.getElementById("empty-results"),
    toast: document.getElementById("toast"),
  };
  let cart = loadCart();
  let toastTimer;

  function loadCart() {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || "{}");
      if (!value || typeof value !== "object" || Array.isArray(value)) return {};
      return Object.fromEntries(Object.entries(value).filter(function (entry) {
        return products.has(entry[0]) && Number.isInteger(entry[1]) && entry[1] > 0 && entry[1] <= 10;
      }));
    } catch (_error) {
      return {};
    }
  }

  function money(amount) {
    return "₹" + (amount / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 });
  }

  function saveCart() {
    try { localStorage.setItem(storageKey, JSON.stringify(cart)); } catch (_error) { /* Cart still works for this visit. */ }
  }

  function showToast(message) {
    elements.toast.textContent = message;
    elements.toast.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () { elements.toast.classList.remove("show"); }, 2300);
  }

  function renderCart() {
    const entries = Object.entries(cart).filter(function (entry) { return products.has(entry[0]); });
    const count = entries.reduce(function (sum, entry) { return sum + entry[1]; }, 0);
    const total = entries.reduce(function (sum, entry) { return sum + products.get(entry[0]).price * entry[1]; }, 0);
    elements.cartCount.textContent = count;
    elements.drawerCount.textContent = "(" + count + ")";
    elements.empty.hidden = count !== 0;
    elements.checkout.hidden = count === 0;
    elements.lines.hidden = count === 0;
    elements.lines.replaceChildren();

    entries.forEach(function (entry) {
      const product = products.get(entry[0]);
      const quantity = entry[1];
      const line = document.createElement("article");
      line.className = "cart-line";
      const image = document.createElement("img");
      image.src = product.image;
      image.alt = "";
      image.loading = "lazy";
      const details = document.createElement("div");
      details.className = "cart-line-details";
      const title = document.createElement("strong");
      title.textContent = product.name;
      const price = document.createElement("span");
      price.textContent = money(product.price);
      details.append(title, price);
      const controls = document.createElement("div");
      controls.className = "quantity-controls";
      const decrease = document.createElement("button");
      decrease.type = "button";
      decrease.textContent = "−";
      decrease.setAttribute("aria-label", "Remove one " + product.name);
      decrease.dataset.quantity = "-1";
      decrease.dataset.id = product.id;
      const amount = document.createElement("span");
      amount.textContent = quantity;
      const increase = document.createElement("button");
      increase.type = "button";
      increase.textContent = "+";
      increase.setAttribute("aria-label", "Add one " + product.name);
      increase.dataset.quantity = "1";
      increase.dataset.id = product.id;
      controls.append(decrease, amount, increase);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "remove-line";
      remove.textContent = "Remove";
      remove.dataset.remove = product.id;
      details.appendChild(remove);
      line.append(image, details, controls);
      elements.lines.appendChild(line);
    });

    elements.total.textContent = money(total);
    const freeDelivery = 250000;
    elements.shippingAway.textContent = total >= freeDelivery ? "free delivery unlocked ✳" : money(freeDelivery - total);
    elements.shippingAway.classList.toggle("shipping-unlocked", total >= freeDelivery);
    elements.checkoutItems.value = JSON.stringify(entries.map(function (entry) {
      return { id: entry[0], quantity: entry[1] };
    }));
    saveCart();
  }

  function openCart() {
    elements.drawer.hidden = false;
    elements.backdrop.hidden = false;
    document.body.classList.add("cart-open");
    document.getElementById("close-cart").focus();
  }

  function closeCart() {
    elements.drawer.hidden = true;
    elements.backdrop.hidden = true;
    document.body.classList.remove("cart-open");
    document.getElementById("open-cart").focus();
  }

  function applyFilters() {
    const query = elements.search.value.trim().toLowerCase();
    const active = document.querySelector(".category-chip.selected");
    const category = active ? active.dataset.category : "All";
    const sort = elements.sort.value;
    const visible = cards.filter(function (card) {
      const matchesText = (card.dataset.name + " " + card.dataset.category + " " + card.textContent).toLowerCase().includes(query);
      const matchesCategory = category === "All" || card.dataset.category === category;
      card.hidden = !matchesText || !matchesCategory;
      return !card.hidden;
    });
    visible.sort(function (a, b) {
      if (sort === "price-low") return Number(a.dataset.price) - Number(b.dataset.price);
      if (sort === "price-high") return Number(b.dataset.price) - Number(a.dataset.price);
      return cards.indexOf(a) - cards.indexOf(b);
    }).forEach(function (card) { elements.grid.appendChild(card); });
    elements.emptyResults.hidden = visible.length !== 0;
    elements.resultCount.textContent = visible.length + (visible.length === 1 ? " lovely find" : " lovely finds");
  }

  document.querySelectorAll("[data-add]").forEach(function (button) {
    button.addEventListener("click", function () {
      const id = button.dataset.add;
      if (!products.has(id)) return;
      cart[id] = Math.min(10, (cart[id] || 0) + 1);
      renderCart();
      showToast(products.get(id).name + " added to your bag ✳");
    });
  });

  elements.lines.addEventListener("click", function (event) {
    const button = event.target.closest("button");
    if (!button) return;
    if (button.dataset.remove) delete cart[button.dataset.remove];
    if (button.dataset.quantity) {
      const id = button.dataset.id;
      cart[id] = Math.max(0, Math.min(10, (cart[id] || 0) + Number(button.dataset.quantity)));
      if (!cart[id]) delete cart[id];
    }
    renderCart();
  });

  document.getElementById("open-cart").addEventListener("click", openCart);
  document.getElementById("close-cart").addEventListener("click", closeCart);
  document.getElementById("continue-shopping").addEventListener("click", closeCart);
  elements.backdrop.addEventListener("click", closeCart);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !elements.drawer.hidden) closeCart();
  });
  elements.search.addEventListener("input", applyFilters);
  elements.sort.addEventListener("change", applyFilters);
  document.querySelectorAll(".category-chip").forEach(function (chip) {
    chip.addEventListener("click", function () {
      document.querySelectorAll(".category-chip").forEach(function (item) { item.classList.remove("selected"); });
      chip.classList.add("selected");
      applyFilters();
    });
  });
  document.querySelectorAll("[data-nav-category]").forEach(function (link) {
    link.addEventListener("click", function () {
      const chip = document.querySelector('.category-chip[data-category="' + link.dataset.navCategory + '"]');
      if (chip) chip.click();
    });
  });
  elements.checkout.addEventListener("submit", function () {
    elements.checkoutItems.value = JSON.stringify(Object.entries(cart).map(function (entry) {
      return { id: entry[0], quantity: entry[1] };
    }));
    localStorage.removeItem(storageKey);
  });
  document.getElementById("newsletter-form").addEventListener("submit", function (event) {
    event.preventDefault();
    document.getElementById("newsletter-note").textContent = "You're on the list — thanks for stopping by! ✳";
    event.currentTarget.reset();
  });

  renderCart();
})();
