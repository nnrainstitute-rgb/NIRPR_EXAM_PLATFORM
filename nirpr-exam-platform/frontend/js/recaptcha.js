// Shared initialization for the v2 checkbox. Configuration errors stay visible.
window.nirprRenderRecaptcha = async function (callbacks = {}) {
  const container = document.getElementById('recaptchaWidget');
  try {
    const response = await fetch('/api/auth/recaptcha-config');
    const config = await response.json();
    if (!response.ok || !config.site_key) {
      throw new Error('Human verification is not configured. Please contact the administrator.');
    }
    for (let attempt = 0; attempt < 100; attempt++) {
      if (window.grecaptcha?.render) {
        return grecaptcha.render(container, {sitekey: config.site_key, theme: 'light', ...callbacks});
      }
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error('Human verification could not load. Check your connection and reload the page.');
  } catch (error) {
    container.textContent = error.message;
    return null;
  }
};
