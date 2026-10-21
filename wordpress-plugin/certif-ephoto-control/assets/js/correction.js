/**
 * Customer correction form: photo preview, signature pad, and a submit guard.
 * No dependency; works without it too (plain file inputs still submit).
 */
(function () {
	'use strict';

	var form = document.querySelector('[data-certif-correction]');
	if (!form) {
		return;
	}

	// Photo preview.
	var photoInput = form.querySelector('[data-preview="photo"]');
	var photoImg = form.querySelector('[data-preview-img="photo"]');
	var photoText = form.querySelector('[data-preview-text="photo"]');
	if (photoInput && photoImg) {
		photoInput.addEventListener('change', function () {
			var file = photoInput.files && photoInput.files[0];
			if (!file) {
				photoImg.hidden = true;
				return;
			}
			photoImg.src = URL.createObjectURL(file);
			photoImg.hidden = false;
			if (photoText) {
				photoText.textContent = file.name;
			}
			hideError();
		});
	}

	// Signature pad: strokes drawn at the canvas' own resolution (2x the 521x134 output).
	var canvas = form.querySelector('[data-signature-pad]');
	var value = form.querySelector('[data-signature-value]');
	var placeholder = form.querySelector('[data-signature-placeholder]');
	var clearBtn = form.querySelector('[data-signature-clear]');
	var signatureFile = form.querySelector('input[name="signature"]');
	var hasInk = false;

	if (canvas && canvas.getContext) {
		var ctx = canvas.getContext('2d');
		var drawing = false;
		var last = null;

		var reset = function () {
			ctx.fillStyle = '#ffffff';
			ctx.fillRect(0, 0, canvas.width, canvas.height);
			hasInk = false;
			if (placeholder) { placeholder.hidden = false; }
			if (clearBtn) { clearBtn.hidden = true; }
			if (value) { value.value = ''; }
		};

		var point = function (event) {
			var rect = canvas.getBoundingClientRect();
			return {
				x: (event.clientX - rect.left) * (canvas.width / rect.width),
				y: (event.clientY - rect.top) * (canvas.height / rect.height)
			};
		};

		reset();
		ctx.lineCap = 'round';
		ctx.lineJoin = 'round';
		ctx.strokeStyle = '#111827';
		ctx.lineWidth = 5;

		canvas.addEventListener('pointerdown', function (event) {
			event.preventDefault();
			drawing = true;
			last = point(event);
			if (canvas.setPointerCapture) {
				canvas.setPointerCapture(event.pointerId);
			}
		});
		canvas.addEventListener('pointermove', function (event) {
			if (!drawing) {
				return;
			}
			var p = point(event);
			ctx.beginPath();
			ctx.moveTo(last.x, last.y);
			ctx.lineTo(p.x, p.y);
			ctx.stroke();
			last = p;
			if (!hasInk) {
				hasInk = true;
				if (placeholder) { placeholder.hidden = true; }
				if (clearBtn) { clearBtn.hidden = false; }
				hideError();
			}
		});
		var stop = function () {
			drawing = false;
			last = null;
		};
		canvas.addEventListener('pointerup', stop);
		canvas.addEventListener('pointercancel', stop);
		canvas.addEventListener('pointerleave', stop);

		if (clearBtn) {
			clearBtn.addEventListener('click', reset);
		}
	}

	// Submit: one click, and at least one new file.
	var errorBox = form.querySelector('[data-form-error]');
	var submit = form.querySelector('[data-submit]');

	var defaultError = errorBox ? errorBox.textContent : '';

	function showError(text) {
		if (!errorBox) {
			return;
		}
		errorBox.textContent = text || defaultError;
		errorBox.hidden = false;
		var smooth = !window.matchMedia || !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
		errorBox.scrollIntoView({ block: 'center', behavior: smooth ? 'smooth' : 'auto' });
	}

	function hideError() {
		if (errorBox) {
			errorBox.hidden = true;
		}
	}

	form.addEventListener('submit', function (event) {
		var hasPhoto = photoInput && photoInput.files && photoInput.files.length > 0;
		var hasSignatureFile = signatureFile && signatureFile.files && signatureFile.files.length > 0;
		var maxBytes = parseInt(form.getAttribute('data-max-bytes'), 10) || 0;
		var tooLarge = maxBytes && ((hasPhoto && photoInput.files[0].size > maxBytes)
			|| (hasSignatureFile && signatureFile.files[0].size > maxBytes));
		if (tooLarge) {
			event.preventDefault();
			showError(form.getAttribute('data-too-large'));
			return;
		}
		if (hasInk && value && !hasSignatureFile) {
			value.value = canvas.toDataURL('image/png');
		}
		if (!hasPhoto && !hasSignatureFile && !hasInk) {
			event.preventDefault();
			showError(defaultError);
			return;
		}
		if (submit) {
			submit.disabled = true;
			var label = submit.querySelector('[data-submit-label]');
			var busy = submit.querySelector('[data-submit-busy]');
			if (label) { label.hidden = true; }
			if (busy) { busy.hidden = false; }
		}
	});
})();
