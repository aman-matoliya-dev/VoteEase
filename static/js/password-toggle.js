document.addEventListener("click", (event) => {
    const target = event.target;
    const button = target instanceof Element
        ? target.closest(".password-toggle")
        : null;
    if (!button) {
        return;
    }

    const input = document.getElementById(button.getAttribute("aria-controls"));
    if (!input) {
        return;
    }

    const isVisible = input.type === "password";
    input.type = isVisible ? "text" : "password";
    button.setAttribute("aria-pressed", String(isVisible));
    button.setAttribute(
        "aria-label",
        isVisible ? "Hide password" : "Show password",
    );
    button.querySelector(".password-toggle__eye").toggleAttribute("hidden", !isVisible);
    button.querySelector(".password-toggle__eye-off").toggleAttribute("hidden", isVisible);
});
