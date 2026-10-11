// ================================
// Mobile Swipe Gestures for Chart Navigation
// ================================
(function() {
    var touchStartX = 0;
    var touchStartY = 0;
    var touchEndX = 0;
    var touchEndY = 0;
    var touchStartInChart = false;
    var minSwipeDistance = 80;
    var maxVerticalDistance = 100;

    function isTouchDevice() {
        return 'ontouchstart' in window || navigator.maxTouchPoints > 0;
    }
    // Expose for mobile action bar
    window.isTouchDevice = isTouchDevice;

    function getVisibleSymbols() {
        // Get all visible symbol cards in order
        var symbols = [];
        var cards = document.querySelectorAll('.watchlist-item:not([style*="display: none"]) .symbol-header .symbol-name');
        cards.forEach(function(el) {
            var text = el.textContent.trim();
            if (text) symbols.push(text);
        });
        return symbols;
    }

    function navigateToSymbol(direction) {
        if (!klineChartSymbol) return;

        // On standalone page, use getOrderedAssetList for navigation
        var symbols = getVisibleSymbols();
        if (symbols.length === 0 && typeof getOrderedAssetList === 'function') {
            symbols = getOrderedAssetList();
        }
        if (symbols.length === 0) return;

        var currentIndex = symbols.indexOf(klineChartSymbol);
        if (currentIndex === -1) return;

        var newIndex;
        if (direction === 'next') {
            newIndex = (currentIndex + 1) % symbols.length;
        } else {
            newIndex = (currentIndex - 1 + symbols.length) % symbols.length;
        }

        var newSymbol = symbols[newIndex];
        if (newSymbol && newSymbol !== klineChartSymbol) {
            openAdvancedChartModal(newSymbol);
            showSwipeHint(newSymbol);
        }
    }
    // Expose for mobile action bar
    window.navigateToSymbol = navigateToSymbol;

    function showSwipeHint(message) {
        var hint = document.querySelector('.swipe-hint');
        if (!hint && klineModal) {
            hint = document.createElement('div');
            hint.className = 'swipe-hint';
            klineModal.appendChild(hint);
        }
        if (hint) {
            hint.textContent = message;
            hint.classList.add('show');
            setTimeout(function() {
                hint.classList.remove('show');
            }, 2000);
        }
    }
    // Expose for mobile action bar
    window.showSwipeHint = showSwipeHint;

    function handleTouchStart(e) {
        touchStartX = e.changedTouches[0].screenX;
        touchStartY = e.changedTouches[0].screenY;
        // Don't swipe-navigate when touch starts on chart canvas, axes, or action bar
        touchStartInChart = !!(e.target.closest('#kline-chart-container') || e.target.closest('.mobile-action-bar') || e.target.closest('.mobile-feature-sheet') || e.target.closest('.mobile-draw-strip') || e.target.closest('.mobile-draw-context') || e.target.closest('.mobile-draw-fab') || e.target.closest('.mds-sheet'));
    }

    function handleTouchEnd(e) {
        touchEndX = e.changedTouches[0].screenX;
        touchEndY = e.changedTouches[0].screenY;
        handleSwipe();
    }

    function handleSwipe() {
        // Only handle swipes in chart modal
        if (!klineModal || !klineModal.classList.contains('visible')) return;
        // Let KLineChart handle chart canvas touches (pan, zoom, axis drag)
        if (touchStartInChart) return;

        var diffX = touchEndX - touchStartX;
        var diffY = touchEndY - touchStartY;

        // Check if horizontal swipe (not vertical scroll)
        if (Math.abs(diffX) > minSwipeDistance && Math.abs(diffY) < maxVerticalDistance) {
            if (diffX > 0) {
                // Swipe right - previous symbol
                navigateToSymbol('prev');
            } else {
                // Swipe left - next symbol
                navigateToSymbol('next');
            }
        }
    }

    // Initialize touch listeners (guard prevents re-registration)
    if (isTouchDevice() && !window._mobileTouchInitDone) {
        window._mobileTouchInitDone = true;
        document.addEventListener('touchstart', handleTouchStart, { passive: true });
        document.addEventListener('touchend', handleTouchEnd, { passive: true });

        // Show initial swipe hint when chart modal opens
        var originalOpenAdvancedChartModal = window.openAdvancedChartModal;
        if (originalOpenAdvancedChartModal) {
            window.openAdvancedChartModal = function(symbol) {
                originalOpenAdvancedChartModal(symbol);
                setTimeout(function() {
                    showSwipeHint('Swipe left/right for next symbol');
                }, 1500);
            };
        }
    }
})();
