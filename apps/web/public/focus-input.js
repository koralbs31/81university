// Distinguish deliberate keyboard navigation from editor/menu autofocus.
(() => {
  const root = document.documentElement;
  const setInput = (value) => {
    if (root.dataset.focusInput !== value) root.dataset.focusInput = value;
  };
  setInput('pointer');
  document.addEventListener('keydown', (event) => {
    if (!event.altKey && !event.ctrlKey && !event.metaKey &&
        (event.key === 'Tab' || event.key.startsWith('Arrow'))) {
      setInput('keyboard');
    }
  }, true);
  document.addEventListener('pointerdown', () => setInput('pointer'), true);
  document.addEventListener('pointermove', () => setInput('pointer'), { capture: true, passive: true });
})();
