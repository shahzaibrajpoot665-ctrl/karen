/* Navbar notifications bell — lazy-loads /api/notifications/ */
$(document).ready(function() {
    if (!document.getElementById('notifBell')) { return; }

    var SEVERITY_ICON = {
        danger: 'mdi-alert-circle text-danger',
        warning: 'mdi-alarm text-warning',
        info: 'mdi-information text-info',
        secondary: 'mdi-clipboard-text text-secondary'
    };
    function esc(s) { return $('<div>').text(s || '').html(); }

    function fetchNotifications(renderList) {
        $.ajax({
            url: '/api/notifications/',
            method: 'GET',
            success: function(resp) {
                var count = resp.count || 0;
                var $badge = $('#notifBadge');
                if (count > 0) {
                    $badge.text(count > 99 ? '99+' : count).show();
                } else {
                    $badge.hide();
                }
                if (renderList) { renderNotifList(resp.notifications || []); }
            }
        });
    }

    function renderNotifList(list) {
        var $box = $('#notifList').empty();
        if (!list.length) {
            $box.append('<a class="dropdown-item text-muted" href="javascript:void(0)"><i class="mdi mdi-check-circle-outline"></i> Nothing pending</a>');
            return;
        }
        list.slice(0, 20).forEach(function(n) {
            var icon = SEVERITY_ICON[n.severity] || SEVERITY_ICON.secondary;
            $box.append(
                '<a class="dropdown-item" href="' + esc(n.url) + '" style="white-space:normal;">' +
                    '<div class="d-flex">' +
                        '<div class="mr-2"><i class="mdi ' + icon + ' font-18"></i></div>' +
                        '<div><div style="font-weight:600;">' + esc(n.title) + '</div>' +
                        '<div class="small text-muted">' + esc(n.text) + '</div></div>' +
                    '</div>' +
                '</a>'
            );
        });
        if (list.length > 20) {
            $box.append('<div class="dropdown-item text-center text-muted small">+' + (list.length - 20) + ' more</div>');
        }
    }

    // Badge count on page load
    fetchNotifications(false);

    // Populate dropdown when opened
    $('#notifBell').on('click', function() {
        fetchNotifications(true);
    });
});
