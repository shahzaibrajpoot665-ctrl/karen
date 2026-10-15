/*
 * Sidebar behaviour — active-item highlighting and mobile handling.
 *
 * - Highlights the sidebar item matching the current URL (including URL
 *   hash, so deep links like /products/#navpills-2 highlight the right
 *   sub-tab) and keeps the parent menu expanded.
 * - Owns submenu expand/collapse itself: sidebarmenu.js's naive toggle
 *   relies on the .active class, which our re-sync manages — letting both
 *   touch it breaks first-click toggling and can leave menus stuck open.
 *   Its handlers are unbound on init so this module is the only authority.
 * - On mobile, closes the off-canvas sidebar when a leaf item is selected
 *   or when the user clicks anywhere outside it.
 */
$(function () {
    "use strict";

    var $nav = $("#sidebarnav");
    if (!$nav.length) { return; }

    var DEFAULT_TAB_HASH = "#navpills-1";

    // The product root treats an empty hash as the default first tab.
    function effectiveHash(pathname, hash) {
        return hash || (pathname === "/products/" ? DEFAULT_TAB_HASH : "");
    }

    function linkUrl(link) {
        try { return new URL(link.href); } catch (e) { return null; }
    }

    function markOpenParents() {
        // A submenu that ended up open keeps its parent link flagged so the
        // open state stays visible (and consistent with aria-expanded).
        $nav.find("ul.in").each(function () {
            $(this).prev("a").addClass("active").attr("aria-expanded", "true");
        });
    }

    function syncActive(preserveOpen) {
        var cur;
        try { cur = new URL(window.location.href); } catch (e) { return; }
        var hash = effectiveHash(cur.pathname, cur.hash);
        var $openUls = preserveOpen ? $nav.find("ul.in") : $();
        var $best = $();

        // 1) exact match: path + search + hash
        $nav.find("a[href]").each(function () {
            var u = linkUrl(this);
            if (!u || u.origin !== cur.origin) { return; }
            if (u.pathname === cur.pathname && u.search === cur.search && u.hash === hash) {
                $best = $(this);
                return false;
            }
        });

        // 2) data-match substring match (e.g. Cart Management on /customer/<id>/cart/)
        if (!$best.length) {
            $nav.find("a[data-match]").each(function () {
                var m = $(this).attr("data-match");
                if (m && cur.pathname.indexOf(m) !== -1) {
                    $best = $(this);
                    return false;
                }
            });
        }

        // 3) pathname-only match (non-root links)
        if (!$best.length) {
            $nav.find("a[href]").each(function () {
                var u = linkUrl(this);
                if (!u || u.origin !== cur.origin) { return; }
                if (u.pathname !== "/" && u.pathname === cur.pathname) {
                    $best = $(this);
                    return false;
                }
            });
        }

        $nav.find("a").removeClass("active").attr("aria-expanded", "false");
        $nav.find("li").removeClass("active selected");
        $nav.find("ul").removeClass("in").attr("aria-expanded", "false");

        if ($best.length) {
            $best.addClass("active");
            // If the match is a parent link, open its submenu too.
            $best.next("ul").addClass("in").attr("aria-expanded", "true");
            $best.parentsUntil(".sidebar-nav").each(function () {
                var $el = $(this);
                if ($el.is("li") && $el.children("a").length !== 0) {
                    $el.children("a").addClass("active").attr("aria-expanded", "true");
                    if ($el.parent("ul#sidebarnav").length === 0) {
                        $el.addClass("active");
                    } else {
                        $el.addClass("selected");
                    }
                } else if ($el.is("ul")) {
                    $el.addClass("in").attr("aria-expanded", "true");
                }
            });
        }

        // Re-open submenus the user expanded manually and flag their parents.
        $openUls.addClass("in").attr("aria-expanded", "true");
        markOpenParents();
    }

    // sidebarmenu.js binds naive click handlers keyed off .active — remove
    // them so this module is the single authority on expand/collapse state.
    $nav.find("a").off("click");

    function closeMobileSidebar() {
        $("#main-wrapper").removeClass("show-sidebar");
        $(".nav-toggler i").addClass("ti-menu");
    }

    // --------------------------------------------------------------
    // Wiring
    // --------------------------------------------------------------

    syncActive();

    $(window).on("hashchange", function () { syncActive(false); });

    $(document).on("click", "#sidebarnav a", function (e) {
        var $a = $(this);
        if ($a.hasClass("has-arrow")) {
            e.preventDefault();
            var $ul = $a.next("ul");
            var opening = !$ul.hasClass("in");
            // accordion: close other open first-level submenus
            $nav.children("li").children("ul.in").not($ul).each(function () {
                $(this).removeClass("in").attr("aria-expanded", "false");
                $(this).prev("a").attr("aria-expanded", "false");
            });
            $ul.toggleClass("in", opening).attr("aria-expanded", String(opening));
            $a.toggleClass("active", opening).attr("aria-expanded", String(opening));
            // keep the current page's highlight consistent after toggling
            syncActive(true);
            return;
        }
        setTimeout(function () { syncActive(false); }, 0);
        closeMobileSidebar();
    });

    // Mobile: clicking anywhere outside the open sidebar closes it.
    $(document).on("click", function (e) {
        if (!$("#main-wrapper").hasClass("show-sidebar")) { return; }
        if ($(e.target).closest(".left-sidebar, .nav-toggler").length) { return; }
        closeMobileSidebar();
    });
});
