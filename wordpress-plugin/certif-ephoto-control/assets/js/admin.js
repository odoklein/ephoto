/**
 * Review dashboard script for Certif ID Ephoto Control.
 *
 * - The browser only ever sends an order id; the server reads the dossier id from the order.
 * - Images go through the authenticated admin-ajax proxy (no service key in the page).
 * - Every modal load carries a token: responses for a previously opened dossier are ignored.
 */
(function ($) {
	'use strict';

	var cfg = window.certifEphoto || {};
	var i18n = cfg.i18n || {};
	var statusLabels = cfg.statusLabels || {};

	var POLL_INTERVAL_MS = 3000;
	var POLL_MAX = 40; // 40 x 3 s = 2 minutes.

	var state = {
		token: 0,
		orderId: 0,
		status: '',
		acceptReady: false,
		busy: false,
		pollTimer: null,
		pollCount: 0
	};

	function t(key) {
		return i18n[key] || '';
	}

	function errorMessage(resp) {
		return resp && resp.data && resp.data.message ? resp.data.message : t('errorOccurred');
	}

	/**
	 * POST to admin-ajax; always resolves with a {success, data} object (also on HTTP 4xx/5xx).
	 */
	function post(data) {
		var deferred = $.Deferred();
		data.nonce = cfg.nonce;
		$.ajax({
			url: cfg.ajaxUrl,
			type: 'POST',
			data: data,
			dataType: 'json'
		}).done(function (resp) {
			if (resp && typeof resp === 'object') {
				deferred.resolve(resp);
			} else {
				deferred.resolve({ success: false, data: { message: t('errorOccurred') } });
			}
		}).fail(function (xhr) {
			var resp = xhr ? xhr.responseJSON : null;
			if (resp && typeof resp === 'object' && resp.data) {
				deferred.resolve(resp);
				return;
			}
			var msg = (xhr && xhr.status === 403) ? t('sessionExpired') : t('errorOccurred');
			deferred.resolve({ success: false, data: { message: msg } });
		});
		return deferred.promise();
	}

	/**
	 * URL of the authenticated image proxy. Built with URLSearchParams so the
	 * cache-buster can never corrupt the other parameters.
	 */
	function imageUrl(orderId, kind) {
		var url;
		try {
			url = new URL(cfg.ajaxUrl, window.location.href);
		} catch (e) {
			return '';
		}
		url.searchParams.set('action', 'certif_ephoto_image');
		url.searchParams.set('order_id', String(orderId));
		url.searchParams.set('kind', kind);
		url.searchParams.set('_wpnonce', cfg.imageNonce || '');
		url.searchParams.set('t', String(Date.now()));
		return url.toString();
	}

	function escapeHtml(str) {
		return $('<div>').text(str === undefined || str === null ? '' : String(str)).html();
	}

	function statusLabel(status) {
		return statusLabels[status] || statusLabels.to_send || status;
	}

	/* ------------------------------------------------------------------
	 * Modal lifecycle
	 * ------------------------------------------------------------------ */

	function stopPolling() {
		if (state.pollTimer) {
			window.clearTimeout(state.pollTimer);
			state.pollTimer = null;
		}
	}

	function resetModal() {
		$('#modal-order-title').text(t('loading'));
		$('#modal-customer-info').text('');
		$('#modal-loading-indicator').show();
		$('#modal-dossier-content').hide();
		$('#modal-error-box').hide().text('');
		$('#modal-status-banner').hide().text('');
		$('#modal-forward-status').text('');
		$('#modal-photo-checks, #modal-sign-checks').empty();
		$('#modal-photo-orig, #modal-photo-clean, #modal-sign-orig, #modal-sign-clean').off('error.certif').removeAttr('src');
		$('#slider-zoom').val('1');
		$('#slider-dx, #slider-dy').val('0');
		$('#rotate-free-angle').val('0');
		updateSliderLabels();
		state.status = '';
		state.acceptReady = false;
		state.busy = false;
		updateActionButtons();
	}

	function openReviewModal(orderId) {
		orderId = parseInt(orderId, 10);
		if (!orderId) {
			return;
		}
		stopPolling();
		state.token += 1;
		state.orderId = orderId;
		state.pollCount = 0;
		resetModal();
		$('#certif-review-modal').fadeIn(150);
		loadReview(state.token, orderId, false);
	}

	function closeReviewModal() {
		stopPolling();
		state.token += 1; // Invalidate any in-flight response.
		state.orderId = 0;
		state.busy = false;
		$('#certif-review-modal').fadeOut(150);
	}

	function loadReview(token, orderId, isPoll) {
		post({ action: 'certif_get_order_review', order_id: orderId }).then(function (resp) {
			if (token !== state.token) {
				return; // Stale: another dossier was opened (or the modal closed) meanwhile.
			}
			if (resp.success && resp.data && resp.data.review) {
				// While polling, keep already loaded images (only load the ones that appear).
				renderReview(resp.data.review, !isPoll);
				if (state.status === 'processing') {
					schedulePoll(token, state.orderId);
				}
				return;
			}
			if (resp.data && resp.data.status) {
				updateRowStatus(orderId, resp.data.status);
			}
			if (isPoll && state.pollCount < POLL_MAX) {
				schedulePoll(token, orderId);
				return;
			}
			showModalError(errorMessage(resp));
		});
	}

	function schedulePoll(token, orderId) {
		stopPolling();
		if (state.pollCount >= POLL_MAX) {
			showBanner('warning', t('processingTimeout'));
			return;
		}
		state.pollCount += 1;
		state.pollTimer = window.setTimeout(function () {
			state.pollTimer = null;
			if (token !== state.token) {
				return;
			}
			loadReview(token, orderId, true);
		}, POLL_INTERVAL_MS);
	}

	function showModalError(message) {
		$('#modal-loading-indicator').hide();
		$('#modal-error-box').text(message).show();
	}

	function showBanner(kind, text) {
		var $banner = $('#modal-status-banner');
		$banner.attr('class', 'certif-modal-banner banner-' + kind).text(text);
		if (text) {
			$banner.show();
		} else {
			$banner.hide();
		}
	}

	/* ------------------------------------------------------------------
	 * Rendering
	 * ------------------------------------------------------------------ */

	function renderReview(review, refreshImages) {
		if (!review || !review.submission) {
			return;
		}
		refreshImages = refreshImages !== false;
		var sub = review.submission;
		var orderId = parseInt(review.order_id, 10) || 0;

		// The order id always comes from the server response.
		state.orderId = orderId;
		state.status = sub.status || '';
		state.acceptReady = sub.accept_ready === true;

		$('#modal-order-title').text(t('titlePrefix') + (review.order_number || orderId));
		var customer = review.customer || {};
		var who = customer.name || '';
		if (customer.email) {
			who += (who ? ' (' : '') + customer.email + (who ? ')' : '');
		}
		$('#modal-customer-info').text(who);

		var files = sub.files || {};
		setImage($('#modal-photo-orig'), orderId, 'photo_original', files.photo_original, refreshImages);
		setImage($('#modal-photo-clean'), orderId, 'photo_clean', files.photo_clean, refreshImages);
		setImage($('#modal-sign-orig'), orderId, 'signature_original', files.signature_original, refreshImages);
		setImage($('#modal-sign-clean'), orderId, 'signature_clean', files.signature_clean, refreshImages);

		var pScore = sub.photo_score !== null && sub.photo_score !== undefined ? sub.photo_score : (sub.photo ? sub.photo.score : null);
		var sScore = sub.signature_score !== null && sub.signature_score !== undefined ? sub.signature_score : (sub.signature ? sub.signature.score : null);
		setScoreBadge($('#modal-photo-score'), pScore);
		setScoreBadge($('#modal-sign-score'), sScore);

		renderChecks($('#modal-photo-checks'), sub.photo);
		renderChecks($('#modal-sign-checks'), sub.signature);

		renderStatus(sub);

		$('#modal-error-box').hide().text('');
		$('#modal-loading-indicator').hide();
		$('#modal-dossier-content').show();

		updateRowStatus(orderId, state.status);
		updateActionButtons();
	}

	function setImage($img, orderId, kind, available, refresh) {
		var $box = $img.closest('.img-box');
		if (available && orderId) {
			$img.off('error.certif').on('error.certif', function () {
				$box.addClass('is-missing');
			});
			if (refresh || !$img.attr('src')) {
				$img.attr('src', imageUrl(orderId, kind));
			}
			$box.removeClass('is-missing');
		} else {
			$img.off('error.certif').removeAttr('src');
			$box.addClass('is-missing');
		}
	}

	function renderChecks($list, report) {
		$list.empty();
		var checks = report && report.checks ? report.checks : [];
		if (report && report.error) {
			$list.append('<div class="check-item status-fail"><span class="check-label">' + escapeHtml(report.error) + '</span></div>');
		}
		if (!checks.length) {
			$list.append('<div class="check-item status-unknown"><span class="check-label">' + escapeHtml(t('noChecks')) + '</span></div>');
			return;
		}
		$.each(checks, function (i, ch) {
			var cls = ch.status === 'pass' ? 'pass' : (ch.status === 'fail' ? 'fail' : 'unknown');
			var badge = cls === 'pass' ? t('checkPass') : (cls === 'fail' ? t('checkFail') : t('checkUnknown'));
			var html = '<div class="check-item status-' + cls + '">' +
				'<span class="check-label">' + escapeHtml(ch.label || ch.key) +
				(ch.detail ? '<small class="check-detail">' + escapeHtml(ch.detail) + '</small>' : '') +
				'</span>' +
				'<span class="check-badge ' + cls + '">' + escapeHtml(badge) + '</span>' +
				'</div>';
			$list.append(html);
		});
	}

	function renderStatus(sub) {
		var st = sub.status || '';
		var when = sub.decided_at ? ' ' + t('decidedOn') + ' ' + sub.decided_at : '';

		if (st === 'processing') {
			showBanner('info', t('processing'));
		} else if (st === 'error') {
			showBanner('error', t('errorStatus') + ' ' + (sub.error || statusLabel('error')));
		} else if (st === 'accepted') {
			showBanner('success', t('acceptedStatus') + when + '.');
		} else if (st === 'rejected') {
			showBanner('error', t('rejectedStatus') + when + '.' + (sub.reviewer_note ? ' ' + t('reasonLabel') + ' ' + sub.reviewer_note : ''));
		} else if (st === 'pending' && !state.acceptReady) {
			showBanner('warning', t('notReady'));
		} else if (st === 'pending') {
			showBanner('info', t('readyToReview'));
		} else {
			showBanner('info', statusLabel(st));
		}

		$('#modal-forward-status').text(sub.forward_status ? t('forwardLabel') + ' ' + sub.forward_status : '');
	}

	function setScoreBadge($el, score) {
		$el.removeClass('score-warning score-fail');
		if (score === null || score === undefined || isNaN(Number(score))) {
			$el.text('--');
			return;
		}
		score = Math.round(Number(score));
		$el.text(score + ' %');
		if (score < 70) {
			$el.addClass('score-fail');
		} else if (score < 90) {
			$el.addClass('score-warning');
		}
	}

	function updateActionButtons() {
		var st = state.status;
		var decided = st === 'accepted' || st === 'rejected';
		var editable = st === 'pending';
		var busy = state.busy;

		$('#btn-accept-dossier').prop('disabled', busy || !(editable && state.acceptReady));
		$('#btn-reject-dossier').prop('disabled', busy || !st || decided || st === 'processing');
		$('#btn-apply-recrop, #btn-reset-recrop, .btn-rotate, #btn-rotate-free').prop('disabled', busy || !editable);
		$('#slider-zoom, #slider-dx, #slider-dy, #rotate-free-angle').prop('disabled', busy || !editable);
	}

	function setBusy(busy) {
		state.busy = !!busy;
		updateActionButtons();
	}

	function updateSliderLabels() {
		$('#val-zoom').text(Number($('#slider-zoom').val()).toFixed(2));
		$('#val-dx').text(Math.round(Number($('#slider-dx').val()) * 100));
		$('#val-dy').text(Math.round(Number($('#slider-dy').val()) * 100));
	}

	function updateRowStatus(orderId, status) {
		if (!status) {
			return;
		}
		var $row = $('#certif-row-' + parseInt(orderId, 10));
		if (!$row.length) {
			return;
		}
		$row.attr('data-status', status);
		$row.removeClass(function (i, cls) {
			return (cls.match(/(^|\s)status-\S+/g) || []).join(' ');
		}).addClass('status-' + status);
		$row.find('.badge-status').attr('class', 'badge-status ' + status).text(statusLabel(status));
	}

	/* ------------------------------------------------------------------
	 * Actions
	 * ------------------------------------------------------------------ */

	/**
	 * Run a modal action; the response is ignored if another dossier was opened meanwhile.
	 */
	function runAction(data, onSuccess) {
		if (!state.orderId || state.busy) {
			return;
		}
		var token = state.token;
		data.order_id = state.orderId;
		setBusy(true);
		post(data).then(function (resp) {
			if (token !== state.token) {
				return;
			}
			setBusy(false);
			if (resp.success) {
				if (resp.data && resp.data.review) {
					renderReview(resp.data.review);
				}
				if (onSuccess) {
					onSuccess(resp.data || {});
				}
				return;
			}
			if (resp.data && resp.data.review) {
				renderReview(resp.data.review);
			} else if (resp.data && resp.data.status) {
				updateRowStatus(state.orderId, resp.data.status);
			}
			window.alert(errorMessage(resp));
		});
	}

	function rotate(angle) {
		angle = parseInt(angle, 10);
		if (isNaN(angle) || angle < -360 || angle > 360) {
			window.alert(t('invalidAngle'));
			return;
		}
		runAction({ action: 'certif_rotate_signature', rotation: angle });
	}

	function refreshProcessingRows() {
		var ids = [];
		$('.certif-row[data-status="processing"]').each(function () {
			ids.push($(this).data('order-id'));
		});
		ids = ids.slice(0, 15);

		(function next() {
			if (!ids.length) {
				return;
			}
			var id = ids.shift();
			post({ action: 'certif_get_order_review', order_id: id }).then(function (resp) {
				if (resp.success && resp.data && resp.data.review && resp.data.review.submission) {
					updateRowStatus(id, resp.data.review.submission.status);
				} else if (resp.data && resp.data.status) {
					updateRowStatus(id, resp.data.status);
				}
				next();
			});
		})();
	}

	$(function () {
		if (!cfg.ajaxUrl) {
			return;
		}

		$(document).on('click', '.btn-examine', function (e) {
			e.preventDefault();
			openReviewModal($(this).data('order-id'));
		});

		// Explicit ingest ("Lancer l'analyse" / "Relancer l'analyse").
		$(document).on('click', '.btn-sync', function (e) {
			e.preventDefault();
			var $btn = $(this);
			var orderId = parseInt($btn.data('order-id'), 10);
			var confirmText = $btn.data('confirm');
			if (!orderId || (confirmText && !window.confirm(confirmText))) {
				return;
			}
			var original = $btn.html();
			$btn.prop('disabled', true).text(t('sending'));

			post({ action: 'certif_sync_order', order_id: orderId }).then(function (resp) {
				$btn.prop('disabled', false).html(original);
				if (resp.success) {
					updateRowStatus(orderId, (resp.data && resp.data.status) || 'processing');
					openReviewModal((resp.data && resp.data.order_id) || orderId);
				} else {
					window.alert(errorMessage(resp));
				}
			});
		});

		$(document).on('click', '.certif-modal-close, .certif-modal-backdrop', function () {
			closeReviewModal();
		});
		$(document).on('keydown', function (e) {
			if (e.key === 'Escape' && $('#certif-review-modal').is(':visible')) {
				closeReviewModal();
			}
		});

		// Recrop (fractions of the crop: zoom 0.5–2, dx/dy -0.5–0.5).
		$('#slider-zoom, #slider-dx, #slider-dy').on('input change', updateSliderLabels);
		$('#btn-reset-recrop').on('click', function () {
			$('#slider-zoom').val('1');
			$('#slider-dx, #slider-dy').val('0');
			updateSliderLabels();
		});
		$('#btn-apply-recrop').on('click', function () {
			runAction({
				action: 'certif_recrop_photo',
				zoom: $('#slider-zoom').val(),
				dx: $('#slider-dx').val(),
				dy: $('#slider-dy').val()
			});
		});

		// Signature rotation (absolute, clockwise, relative to the original).
		$(document).on('click', '.btn-rotate', function () {
			rotate($(this).data('angle'));
		});
		$('#btn-rotate-free').on('click', function () {
			rotate($('#rotate-free-angle').val());
		});

		// Accept.
		$('#btn-accept-dossier').on('click', function () {
			if (!state.acceptReady || state.status !== 'pending') {
				return;
			}
			if (!window.confirm(t('confirmAccept'))) {
				return;
			}
			var orderId = state.orderId;
			runAction({ action: 'certif_validate_order', action_type: 'accept' }, function (data) {
				updateRowStatus(orderId, data.status || 'accepted');
				window.alert(t('acceptedSuccess'));
				if (!data.review) {
					closeReviewModal();
				}
			});
		});

		// Reject.
		$('#btn-reject-dossier').on('click', function () {
			var reason = window.prompt(t('rejectPrompt'));
			if (reason === null) {
				return;
			}
			reason = String(reason).trim();
			if (reason === '') {
				window.alert(t('rejectEmpty'));
				return;
			}
			var orderId = state.orderId;
			runAction({ action: 'certif_validate_order', action_type: 'reject', reason: reason }, function (data) {
				updateRowStatus(orderId, data.status || 'rejected');
				window.alert(t('rejectedSuccess'));
				if (!data.review) {
					closeReviewModal();
				}
			});
		});

		// ?order_id= only OPENS the dossier (view only, never triggers an ingest).
		var params = new URLSearchParams(window.location.search);
		var paramOrderId = params.get('order_id');
		if (paramOrderId && /^\d+$/.test(paramOrderId) && $('#certif-review-modal').length) {
			openReviewModal(paramOrderId);
		}

		refreshProcessingRows();
	});

})(jQuery);
