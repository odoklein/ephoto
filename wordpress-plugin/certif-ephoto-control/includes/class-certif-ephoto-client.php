<?php
/**
 * API client for the Python control service.
 *
 * Authentication is sent in headers only (X-API-Key, X-Reviewer-User).
 * No key is ever put in a URL or sent to the browser.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Certif_Ephoto_Client {

	/**
	 * Statuses the service can report for a dossier.
	 */
	const SERVICE_STATUSES = array( 'processing', 'pending', 'accepted', 'rejected', 'error' );

	/**
	 * File kinds served by GET /api/v1/files/{id}/{kind}.
	 */
	const FILE_KINDS = array( 'photo_original', 'photo_clean', 'signature_original', 'signature_clean' );

	/**
	 * Every order meta key written by the plugin.
	 */
	public static function meta_keys() {
		return array(
			'_certif_ephoto_submission_id',
			'_certif_ephoto_status',
			'_certif_ephoto_error',
			'_certif_ephoto_forward_status',
			'_certif_ephoto_accept_ready',
			'_certif_ephoto_photo_url',
			'_certif_ephoto_signature_url',
			'_certif_ephoto_ingested_at',
			'_certif_ephoto_decided_at',
			'_certif_ephoto_reviewer',
			'_certif_ephoto_reason',
		);
	}

	/* ------------------------------------------------------------------
	 * Configuration
	 * ------------------------------------------------------------------ */

	/**
	 * Raw configured service URL (constant first, then option), not validated.
	 */
	public static function get_configured_service_url() {
		$url = defined( 'CERTIF_EPHOTO_SERVICE_URL' )
			? CERTIF_EPHOTO_SERVICE_URL
			: get_option( 'certif_ephoto_service_url', '' );
		return untrailingslashit( esc_url_raw( trim( (string) $url ), array( 'http', 'https' ) ) );
	}

	/**
	 * Usable service URL: empty string when missing or not allowed (non-https).
	 */
	public static function get_service_url() {
		$url = self::get_configured_service_url();
		if ( '' === $url || ! self::is_allowed_service_url( $url ) ) {
			return '';
		}
		return $url;
	}

	/**
	 * https is required; http only for localhost / 127.0.0.1 / ::1.
	 */
	public static function is_allowed_service_url( $url ) {
		$parts = wp_parse_url( (string) $url );
		if ( ! is_array( $parts ) || empty( $parts['scheme'] ) || empty( $parts['host'] ) ) {
			return false;
		}
		if ( isset( $parts['user'] ) || isset( $parts['pass'] ) ) {
			return false;
		}
		$scheme = strtolower( $parts['scheme'] );
		$host   = strtolower( trim( $parts['host'], '[]' ) );
		if ( 'https' === $scheme ) {
			return true;
		}
		if ( 'http' === $scheme && in_array( $host, array( 'localhost', '127.0.0.1', '::1' ), true ) ) {
			return true;
		}
		if ( 'http' === $scheme ) {
			/**
			 * Opt-in for a plain-http service on a private network (e.g. a Docker
			 * service name). The API keys then travel unencrypted on that network.
			 */
			return (bool) apply_filters( 'certif_ephoto_allow_insecure_service_url', false, $url );
		}
		return false;
	}

	/**
	 * Ingestion key (service INGEST_API_KEY).
	 */
	public static function get_api_key() {
		$key = defined( 'CERTIF_EPHOTO_API_KEY' )
			? CERTIF_EPHOTO_API_KEY
			: get_option( 'certif_ephoto_api_key', '' );
		return trim( (string) $key );
	}

	/**
	 * Reviewer key (service REVIEW_API_KEY). Falls back to the ingestion key.
	 */
	public static function get_review_key() {
		$key = defined( 'CERTIF_EPHOTO_REVIEW_API_KEY' )
			? CERTIF_EPHOTO_REVIEW_API_KEY
			: get_option( 'certif_ephoto_review_key', '' );
		$key = trim( (string) $key );
		return '' !== $key ? $key : self::get_api_key();
	}

	/**
	 * Login of the current WordPress user, for the X-Reviewer-User header.
	 */
	public static function reviewer_login() {
		$login = '';
		$user  = wp_get_current_user();
		if ( $user && $user->exists() ) {
			$login = (string) $user->user_login;
		}
		$login = preg_replace( '/[^\x20-\x7E]/', '', $login );
		$login = trim( (string) $login );
		if ( '' === $login ) {
			$login = ( $user && $user->exists() ) ? 'wp-user-' . (int) $user->ID : 'wordpress';
		}
		return substr( $login, 0, 64 );
	}

	/**
	 * Human-readable name of the current user (order notes only).
	 */
	public static function reviewer_label() {
		$user = wp_get_current_user();
		if ( $user && $user->exists() ) {
			return '' !== (string) $user->display_name ? (string) $user->display_name : (string) $user->user_login;
		}
		return __( 'contrôleur', 'certif-ephoto-control' );
	}

	/**
	 * Submission ids come from the service; keep them to a safe charset.
	 */
	public static function clean_submission_id( $submission_id ) {
		$submission_id = is_scalar( $submission_id ) ? trim( (string) $submission_id ) : '';
		return preg_match( '/^[A-Za-z0-9_-]{1,100}$/', $submission_id ) ? $submission_id : '';
	}

	/* ------------------------------------------------------------------
	 * HTTP helpers
	 * ------------------------------------------------------------------ */

	/**
	 * Perform an authenticated request to the service.
	 *
	 * @param string $method GET|POST.
	 * @param string $path   Path starting with "/".
	 * @param array  $args   wp_remote_request() args.
	 * @param string $auth   'ingest' (api_key) or 'review' (review_key + X-Reviewer-User).
	 * @return array|WP_Error Raw HTTP response.
	 */
	private static function request( $method, $path, $args = array(), $auth = 'review' ) {
		$base = self::get_service_url();
		if ( '' === $base ) {
			if ( '' !== self::get_configured_service_url() ) {
				return new WP_Error( 'insecure_service_url', __( 'L’adresse du service doit être en https (http accepté uniquement pour localhost / 127.0.0.1).', 'certif-ephoto-control' ) );
			}
			return new WP_Error( 'not_configured', __( 'L’adresse du service de contrôle n’est pas configurée.', 'certif-ephoto-control' ) );
		}

		$key = 'ingest' === $auth ? self::get_api_key() : self::get_review_key();
		if ( '' === $key ) {
			return new WP_Error( 'no_api_key', __( 'Aucune clé d’API n’est configurée pour le service de contrôle.', 'certif-ephoto-control' ) );
		}

		$headers = isset( $args['headers'] ) && is_array( $args['headers'] ) ? $args['headers'] : array();
		$headers['X-API-Key'] = $key;
		if ( 'review' === $auth ) {
			$headers['X-Reviewer-User'] = self::reviewer_login();
		}
		if ( ! isset( $headers['Accept'] ) ) {
			$headers['Accept'] = 'application/json';
		}

		$args['headers']     = $headers;
		$args['method']      = $method;
		$args['redirection'] = 0; // Never replay the key to a redirect target.
		if ( ! isset( $args['timeout'] ) ) {
			$args['timeout'] = 15;
		}
		$args['user-agent'] = 'CertifEphotoControl/' . CERTIF_EPHOTO_VERSION . '; ' . home_url( '/' );

		$response = wp_remote_request( $base . $path, $args );
		if ( is_wp_error( $response ) ) {
			return new WP_Error(
				'service_unreachable',
				sprintf( __( 'Service de contrôle injoignable : %s', 'certif-ephoto-control' ), $response->get_error_message() ),
				array( 'status' => 0 )
			);
		}
		return $response;
	}

	/**
	 * Decode a JSON body; returns null when not JSON.
	 */
	private static function decode( $response ) {
		$data = json_decode( (string) wp_remote_retrieve_body( $response ), true );
		return is_array( $data ) ? $data : null;
	}

	/**
	 * Is the HTTP code a 2xx?
	 */
	private static function is_success( $code ) {
		$code = (int) $code;
		return $code >= 200 && $code < 300;
	}

	/**
	 * Build a WP_Error from a non-2xx response, using the service "detail".
	 */
	private static function http_error( $error_code, $context, $response ) {
		$status = (int) wp_remote_retrieve_response_code( $response );
		$raw    = (string) wp_remote_retrieve_body( $response );
		$body   = json_decode( $raw, true );
		$detail = self::error_detail( $body, $raw );

		if ( 401 === $status || 403 === $status ) {
			$message = sprintf( __( '%1$s : clé d’API refusée par le service (HTTP %2$d). Vérifiez les réglages.', 'certif-ephoto-control' ), $context, $status );
		} elseif ( '' !== $detail ) {
			$message = sprintf( __( '%1$s (HTTP %2$d) : %3$s', 'certif-ephoto-control' ), $context, $status, $detail );
		} else {
			$message = sprintf( __( '%1$s (HTTP %2$d).', 'certif-ephoto-control' ), $context, $status );
		}

		return new WP_Error( $error_code, $message, array( 'status' => $status, 'detail' => $detail ) );
	}

	/**
	 * Extract a readable message from a FastAPI error body.
	 * "detail" may be a string, a list of validation errors ({loc, msg}) or an object.
	 */
	public static function error_detail( $body, $raw = '' ) {
		$detail = '';
		if ( is_array( $body ) && array_key_exists( 'detail', $body ) ) {
			$detail = self::stringify_detail( $body['detail'] );
		}
		if ( '' === $detail && is_string( $raw ) && ! is_array( $body ) ) {
			$detail = wp_strip_all_tags( $raw );
		}
		$detail = trim( (string) preg_replace( '/\s+/', ' ', $detail ) );
		if ( function_exists( 'mb_strlen' ) && mb_strlen( $detail, 'UTF-8' ) > 300 ) {
			$detail = mb_substr( $detail, 0, 300, 'UTF-8' ) . '…';
		} elseif ( strlen( $detail ) > 300 ) {
			$detail = substr( $detail, 0, 300 ) . '…';
		}
		return $detail;
	}

	/**
	 * Turn a "detail" value (string|array|object) into a string, never "Array".
	 */
	private static function stringify_detail( $detail ) {
		if ( is_string( $detail ) || is_int( $detail ) || is_float( $detail ) ) {
			return (string) $detail;
		}
		if ( ! is_array( $detail ) ) {
			return '';
		}

		$parts = array();
		foreach ( $detail as $key => $item ) {
			if ( is_array( $item ) ) {
				if ( isset( $item['msg'] ) && is_scalar( $item['msg'] ) ) {
					$msg = (string) $item['msg'];
					if ( isset( $item['loc'] ) && is_array( $item['loc'] ) ) {
						$loc = array();
						foreach ( $item['loc'] as $segment ) {
							if ( is_scalar( $segment ) && 'body' !== $segment ) {
								$loc[] = (string) $segment;
							}
						}
						if ( ! empty( $loc ) ) {
							$msg = implode( '.', $loc ) . ' : ' . $msg;
						}
					}
					$parts[] = $msg;
				} elseif ( isset( $item['message'] ) && is_scalar( $item['message'] ) ) {
					$parts[] = (string) $item['message'];
				} else {
					$encoded = wp_json_encode( $item );
					if ( is_string( $encoded ) ) {
						$parts[] = $encoded;
					}
				}
			} elseif ( is_scalar( $item ) ) {
				$parts[] = is_string( $key ) ? $key . ' : ' . (string) $item : (string) $item;
			}
		}
		return implode( ' ; ', $parts );
	}

	/**
	 * HTTP status attached to a WP_Error built by this class (0 if none).
	 */
	public static function error_status( $error ) {
		if ( ! is_wp_error( $error ) ) {
			return 0;
		}
		$data = $error->get_error_data();
		return ( is_array( $data ) && isset( $data['status'] ) ) ? (int) $data['status'] : 0;
	}

	/**
	 * Format a float for the service without locale side effects ("0.5", never "0,5").
	 */
	private static function format_float( $value ) {
		$out = rtrim( rtrim( sprintf( '%.4F', (float) $value ), '0' ), '.' );
		return ( '' === $out || '-0' === $out ) ? '0' : $out;
	}

	/**
	 * Sanitize a short scalar for storage in order meta.
	 */
	private static function short_text( $value, $max = 500 ) {
		if ( ! is_scalar( $value ) ) {
			return '';
		}
		$value = sanitize_text_field( (string) $value );
		if ( function_exists( 'mb_substr' ) ) {
			return mb_substr( $value, 0, $max, 'UTF-8' );
		}
		return substr( $value, 0, $max );
	}

	/* ------------------------------------------------------------------
	 * Health / diagnostics
	 * ------------------------------------------------------------------ */

	/**
	 * Public health check (GET /api/health). Only run on explicit request.
	 */
	public static function test_connection() {
		$base = self::get_service_url();
		if ( '' === $base ) {
			return array(
				'ok'      => false,
				'message' => '' !== self::get_configured_service_url()
					? __( 'L’adresse du service doit être en https (http accepté uniquement pour localhost / 127.0.0.1).', 'certif-ephoto-control' )
					: __( 'L’adresse du service n’est pas configurée.', 'certif-ephoto-control' ),
			);
		}

		$response = wp_remote_get(
			$base . '/api/health',
			array(
				'timeout'     => 8,
				'redirection' => 0,
				'headers'     => array( 'Accept' => 'application/json' ),
			)
		);

		if ( is_wp_error( $response ) ) {
			return array(
				'ok'      => false,
				'message' => $response->get_error_message(),
			);
		}

		$code = (int) wp_remote_retrieve_response_code( $response );
		if ( 200 !== $code ) {
			return array(
				'ok'      => false,
				'message' => sprintf( __( 'Code de réponse HTTP inattendu : %d', 'certif-ephoto-control' ), $code ),
			);
		}

		$body   = self::decode( $response );
		$status = ( is_array( $body ) && isset( $body['status'] ) && is_string( $body['status'] ) ) ? $body['status'] : '';
		return array(
			'ok'       => true,
			'status'   => '' !== $status ? $status : 'inconnu',
			'message'  => 'degraded' === $status
				? __( 'Service joignable mais en mode dégradé.', 'certif-ephoto-control' )
				: __( 'Service opérationnel.', 'certif-ephoto-control' ),
			'detector' => ( is_array( $body ) && isset( $body['face_detector'] ) && is_scalar( $body['face_detector'] ) ) ? (string) $body['face_detector'] : 'inconnu',
		);
	}

	/**
	 * Check both keys against an authenticated endpoint (unknown id => 404 = key accepted).
	 * The review key is probed on a reviewer-only endpoint (files), because the
	 * service refuses the ingestion key there.
	 *
	 * @return array{ingest: array, review: array}
	 */
	public static function test_keys() {
		$results = array();
		$unknown = rawurlencode( '00000000-0000-0000-0000-000000000000' );
		$probes  = array(
			'ingest' => '/api/v1/submissions/' . $unknown,
			'review' => '/api/v1/files/' . $unknown . '/photo_original',
		);
		foreach ( $probes as $auth => $probe ) {
			$response = self::request( 'GET', $probe, array( 'timeout' => 8 ), $auth );
			if ( is_wp_error( $response ) ) {
				$results[ $auth ] = array( 'ok' => false, 'message' => $response->get_error_message() );
				continue;
			}
			$code = (int) wp_remote_retrieve_response_code( $response );
			if ( 404 === $code || self::is_success( $code ) ) {
				$results[ $auth ] = array( 'ok' => true, 'message' => __( 'Clé acceptée.', 'certif-ephoto-control' ) );
			} elseif ( 401 === $code || 403 === $code ) {
				$results[ $auth ] = array( 'ok' => false, 'message' => sprintf( __( 'Clé refusée (HTTP %d).', 'certif-ephoto-control' ), $code ) );
			} elseif ( 503 === $code ) {
				$results[ $auth ] = array( 'ok' => false, 'message' => __( 'Clé non configurée sur le service (HTTP 503).', 'certif-ephoto-control' ) );
			} else {
				$results[ $auth ] = array( 'ok' => false, 'message' => sprintf( __( 'Réponse inattendue (HTTP %d).', 'certif-ephoto-control' ), $code ) );
			}
		}
		return $results;
	}

	/* ------------------------------------------------------------------
	 * Ingestion
	 * ------------------------------------------------------------------ */

	/**
	 * Send an order to the service.
	 *
	 * Re-ingestion is only allowed when there is no submission, or when the
	 * existing one is rejected/error, or unknown to the service (404, purged).
	 * A live dossier (processing/pending/accepted) is never re-sent.
	 *
	 * @param int  $order_id WooCommerce order id.
	 * @param bool $manual   True when triggered by a reviewer click (reviewer key,
	 *                       re-ingest of a rejected dossier allowed).
	 * @return array|WP_Error
	 */
	public static function ingest_order( $order_id, $manual = false ) {
		$order_id = absint( $order_id );
		$order    = $order_id && function_exists( 'wc_get_order' ) ? wc_get_order( $order_id ) : false;
		if ( ! $order instanceof WC_Order ) {
			return new WP_Error( 'order_not_found', __( 'Commande introuvable.', 'certif-ephoto-control' ) );
		}

		$lock = 'certif_ephoto_ingest_lock_' . $order_id;
		if ( false !== get_transient( $lock ) ) {
			return new WP_Error( 'ingest_locked', __( 'Un envoi est déjà en cours pour cette commande. Réessayez dans quelques instants.', 'certif-ephoto-control' ) );
		}
		set_transient( $lock, time(), 120 );

		try {
			return self::do_ingest( $order, (bool) $manual );
		} finally {
			delete_transient( $lock );
		}
	}

	/**
	 * Ingestion body (called under the per-order lock).
	 */
	private static function do_ingest( $order, $manual ) {
		$existing = self::clean_submission_id( $order->get_meta( '_certif_ephoto_submission_id' ) );

		if ( '' !== $existing ) {
			$remote = self::get_submission( $existing, $manual );
			if ( is_wp_error( $remote ) ) {
				if ( 404 !== self::error_status( $remote ) ) {
					return new WP_Error(
						'existing_unverifiable',
						sprintf( __( 'Impossible de vérifier le dossier existant, envoi annulé : %s', 'certif-ephoto-control' ), $remote->get_error_message() )
					);
				}
				// 404: unknown or purged on the service -> a new submission is allowed.
			} else {
				self::store_submission_state( $order, $remote );
				$current = isset( $remote['status'] ) && is_string( $remote['status'] ) ? $remote['status'] : '';
				$live    = in_array( $current, array( 'processing', 'pending', 'accepted' ), true );
				if ( $live || ( ! $manual && 'rejected' === $current ) ) {
					return array(
						'submission_id' => $existing,
						'status'        => $current,
						'existing'      => true,
						'duplicate'     => false,
					);
				}
			}
		}

		$extracted = Certif_Ephoto_Order_Reader::extract_dossier_data( $order );
		if ( is_wp_error( $extracted ) ) {
			return $extracted;
		}

		if ( empty( $extracted['photo_url'] ) || empty( $extracted['signature_url'] ) ) {
			$missing = array();
			if ( empty( $extracted['photo_url'] ) ) {
				$missing[] = 'photo';
			}
			if ( empty( $extracted['signature_url'] ) ) {
				$missing[] = 'signature';
			}
			return new WP_Error(
				'missing_files',
				sprintf( __( 'Fichiers manquants dans la commande : %s', 'certif-ephoto-control' ), implode( ', ', $missing ) )
			);
		}

		$payload = array(
			'order_id'  => (string) $order->get_order_number(),
			'customer'  => array(
				'name'  => (string) $extracted['customer']['name'],
				'email' => (string) $extracted['customer']['email'],
			),
			'photo'     => array( 'url' => $extracted['photo_url'] ),
			'signature' => array( 'url' => $extracted['signature_url'] ),
		);

		if ( function_exists( 'wc_set_time_limit' ) ) {
			wc_set_time_limit( 120 );
		}

		$response = self::request(
			'POST',
			'/api/v1/ingest',
			array(
				'timeout' => 60, // The service downloads both images synchronously.
				'headers' => array( 'Content-Type' => 'application/json' ),
				'body'    => wp_json_encode( $payload ),
			),
			'ingest'
		);
		if ( is_wp_error( $response ) ) {
			return $response;
		}

		$code = (int) wp_remote_retrieve_response_code( $response );
		if ( ! self::is_success( $code ) ) {
			return self::http_error( 'ingest_failed', __( 'Envoi refusé par le service', 'certif-ephoto-control' ), $response );
		}

		$body          = self::decode( $response );
		$submission_id = is_array( $body ) && isset( $body['submission_id'] ) ? self::clean_submission_id( $body['submission_id'] ) : '';
		if ( '' === $submission_id ) {
			return new WP_Error( 'no_submission_id', __( 'Réponse invalide du service de contrôle (identifiant de dossier absent).', 'certif-ephoto-control' ) );
		}

		$duplicate = ! empty( $body['duplicate'] );
		$status    = ( isset( $body['status'] ) && is_string( $body['status'] ) && in_array( $body['status'], self::SERVICE_STATUSES, true ) )
			? $body['status']
			: 'processing';

		if ( $submission_id !== $existing ) {
			// New dossier: forget the state of the previous one.
			foreach ( array( '_certif_ephoto_status', '_certif_ephoto_error', '_certif_ephoto_forward_status', '_certif_ephoto_accept_ready', '_certif_ephoto_decided_at', '_certif_ephoto_reviewer', '_certif_ephoto_reason' ) as $meta_key ) {
				$order->delete_meta_data( $meta_key );
			}
		}
		$order->update_meta_data( '_certif_ephoto_submission_id', $submission_id );
		$order->update_meta_data( '_certif_ephoto_photo_url', $extracted['photo_url'] );
		$order->update_meta_data( '_certif_ephoto_signature_url', $extracted['signature_url'] );
		$order->update_meta_data( '_certif_ephoto_ingested_at', current_time( 'mysql' ) );
		if ( ! $duplicate ) {
			$order->update_meta_data( '_certif_ephoto_status', $status );
		}
		$order->save();

		if ( $duplicate ) {
			// An existing dossier was returned: sync its real state (may already be decided).
			$remote = self::get_submission( $submission_id, $manual );
			if ( ! is_wp_error( $remote ) ) {
				self::store_submission_state( $order, $remote );
				if ( isset( $remote['status'] ) && is_string( $remote['status'] ) && in_array( $remote['status'], self::SERVICE_STATUSES, true ) ) {
					$status = $remote['status'];
				}
			} else {
				$order->update_meta_data( '_certif_ephoto_status', $status );
				$order->save();
			}
			$order->add_order_note(
				sprintf( __( '[Certif ID] Dossier déjà présent sur le service, rattaché à la commande (ID : %s).', 'certif-ephoto-control' ), $submission_id )
			);
		} else {
			$order->add_order_note(
				sprintf( __( '[Certif ID] Dossier soumis au contrôle photo & signature (ID : %s).', 'certif-ephoto-control' ), $submission_id )
			);
		}

		return array(
			'submission_id' => $submission_id,
			'status'        => $status,
			'existing'      => false,
			'duplicate'     => $duplicate,
		);
	}

	/* ------------------------------------------------------------------
	 * Review endpoints
	 * ------------------------------------------------------------------ */

	/**
	 * GET /api/v1/submissions/{id}.
	 *
	 * @param string $submission_id
	 * @param bool   $as_reviewer True: reviewer key + X-Reviewer-User. False: ingestion key (background jobs).
	 * @return array|WP_Error WP_Error data status 404 when unknown or purged.
	 */
	public static function get_submission( $submission_id, $as_reviewer = true ) {
		$submission_id = self::clean_submission_id( $submission_id );
		if ( '' === $submission_id ) {
			return new WP_Error( 'invalid_submission_id', __( 'Identifiant de dossier invalide.', 'certif-ephoto-control' ) );
		}

		$response = self::request(
			'GET',
			'/api/v1/submissions/' . rawurlencode( $submission_id ),
			array( 'timeout' => 15 ),
			$as_reviewer ? 'review' : 'ingest'
		);
		if ( is_wp_error( $response ) ) {
			return $response;
		}

		$code = (int) wp_remote_retrieve_response_code( $response );
		if ( 404 === $code ) {
			return new WP_Error(
				'submission_not_found',
				__( 'Dossier introuvable sur le service (inconnu ou purgé 30 jours après décision).', 'certif-ephoto-control' ),
				array( 'status' => 404 )
			);
		}
		if ( ! self::is_success( $code ) ) {
			return self::http_error( 'submission_error', __( 'Impossible de récupérer le dossier', 'certif-ephoto-control' ), $response );
		}

		$data = self::decode( $response );
		if ( ! is_array( $data ) ) {
			return new WP_Error( 'invalid_json', __( 'Format de réponse invalide.', 'certif-ephoto-control' ) );
		}
		return $data;
	}

	/**
	 * GET /api/v1/files/{id}/{kind} (server side only, for the image proxy).
	 *
	 * @return array{content_type: string, body: string}|WP_Error
	 */
	public static function fetch_file( $submission_id, $kind ) {
		$submission_id = self::clean_submission_id( $submission_id );
		if ( '' === $submission_id || ! in_array( $kind, self::FILE_KINDS, true ) ) {
			return new WP_Error( 'invalid_request', __( 'Requête invalide.', 'certif-ephoto-control' ), array( 'status' => 400 ) );
		}

		$response = self::request(
			'GET',
			'/api/v1/files/' . rawurlencode( $submission_id ) . '/' . rawurlencode( $kind ),
			array(
				'timeout'             => 20,
				'limit_response_size' => 20 * 1024 * 1024,
				'headers'             => array( 'Accept' => 'image/jpeg, image/png, image/webp' ),
			),
			'review'
		);
		if ( is_wp_error( $response ) ) {
			return $response;
		}

		$code = (int) wp_remote_retrieve_response_code( $response );
		if ( ! self::is_success( $code ) ) {
			return self::http_error( 'file_error', __( 'Fichier indisponible', 'certif-ephoto-control' ), $response );
		}

		$type = wp_remote_retrieve_header( $response, 'content-type' );
		if ( is_array( $type ) ) {
			$type = reset( $type );
		}

		return array(
			'content_type' => (string) $type,
			'body'         => (string) wp_remote_retrieve_body( $response ),
		);
	}

	/**
	 * POST /api/v1/submissions/{id}/recrop. Values are fractions of the crop.
	 *
	 * @param string $submission_id
	 * @param float  $zoom 0.5 .. 2.0 (> 1 makes the face smaller).
	 * @param float  $dx   -0.5 .. 0.5 (negative = left).
	 * @param float  $dy   -0.5 .. 0.5 (negative = up).
	 * @return array|WP_Error
	 */
	public static function recrop( $submission_id, $zoom = 1.0, $dx = 0.0, $dy = 0.0 ) {
		$submission_id = self::clean_submission_id( $submission_id );
		if ( '' === $submission_id ) {
			return new WP_Error( 'invalid_submission_id', __( 'Identifiant de dossier invalide.', 'certif-ephoto-control' ) );
		}

		$response = self::request(
			'POST',
			'/api/v1/submissions/' . rawurlencode( $submission_id ) . '/recrop',
			array(
				'timeout' => 30,
				'body'    => array(
					'zoom' => self::format_float( $zoom ),
					'dx'   => self::format_float( $dx ),
					'dy'   => self::format_float( $dy ),
				),
			),
			'review'
		);
		if ( is_wp_error( $response ) ) {
			return $response;
		}

		$code = (int) wp_remote_retrieve_response_code( $response );
		if ( ! self::is_success( $code ) ) {
			return self::http_error( 'recrop_failed', __( 'Recadrage refusé', 'certif-ephoto-control' ), $response );
		}

		$body = self::decode( $response );
		return is_array( $body ) ? $body : array( 'status' => 'ok' );
	}

	/**
	 * POST /api/v1/submissions/{id}/rotate-signature.
	 *
	 * @param string $submission_id
	 * @param int    $rotation Clockwise degrees, may be negative.
	 * @return array|WP_Error
	 */
	public static function rotate_signature( $submission_id, $rotation ) {
		$submission_id = self::clean_submission_id( $submission_id );
		if ( '' === $submission_id ) {
			return new WP_Error( 'invalid_submission_id', __( 'Identifiant de dossier invalide.', 'certif-ephoto-control' ) );
		}

		$response = self::request(
			'POST',
			'/api/v1/submissions/' . rawurlencode( $submission_id ) . '/rotate-signature',
			array(
				'timeout' => 30,
				'body'    => array( 'rotation' => (string) intval( $rotation ) ),
			),
			'review'
		);
		if ( is_wp_error( $response ) ) {
			return $response;
		}

		$code = (int) wp_remote_retrieve_response_code( $response );
		if ( ! self::is_success( $code ) ) {
			return self::http_error( 'rotate_failed', __( 'Rotation refusée', 'certif-ephoto-control' ), $response );
		}

		$body = self::decode( $response );
		return is_array( $body ) ? $body : array( 'status' => 'ok' );
	}

	/**
	 * POST /api/v1/validate/{id}. Only talks to the service; the caller updates
	 * the order (see apply_decision()).
	 *
	 * @param string $submission_id
	 * @param string $action 'accept' or 'reject'.
	 * @param string $reason Required for reject.
	 * @return array|WP_Error WP_Error data carries the HTTP status (409, 422, 502...).
	 */
	public static function validate( $submission_id, $action, $reason = '' ) {
		$submission_id = self::clean_submission_id( $submission_id );
		if ( '' === $submission_id || ! in_array( $action, array( 'accept', 'reject' ), true ) ) {
			return new WP_Error( 'invalid_request', __( 'Paramètres invalides.', 'certif-ephoto-control' ) );
		}

		$response = self::request(
			'POST',
			'/api/v1/validate/' . rawurlencode( $submission_id ),
			array(
				'timeout' => 45, // The service calls the Make webhook synchronously.
				'headers' => array( 'Content-Type' => 'application/json' ),
				'body'    => wp_json_encode(
					array(
						'action' => $action,
						'reason' => (string) $reason,
					)
				),
			),
			'review'
		);
		if ( is_wp_error( $response ) ) {
			return $response;
		}

		$code = (int) wp_remote_retrieve_response_code( $response );
		if ( ! self::is_success( $code ) ) {
			$context = 502 === $code
				? __( 'La transmission vers Make a échoué ; le dossier reste à contrôler et vous pouvez réessayer', 'certif-ephoto-control' )
				: __( 'Décision refusée par le service', 'certif-ephoto-control' );
			return self::http_error( 'validate_failed', $context, $response );
		}

		$body = self::decode( $response );
		return is_array( $body ) ? $body : array();
	}

	/* ------------------------------------------------------------------
	 * Order state
	 * ------------------------------------------------------------------ */

	/**
	 * Store the service state of a dossier in the order meta. When the service
	 * says accepted/rejected and the order does not reflect it yet, the
	 * WooCommerce order is reconciled (status + note).
	 *
	 * @param WC_Order $order
	 * @param array    $data Body of GET /api/v1/submissions/{id}.
	 */
	public static function store_submission_state( $order, $data ) {
		if ( ! $order instanceof WC_Order || ! is_array( $data ) ) {
			return;
		}

		$status = ( isset( $data['status'] ) && is_string( $data['status'] ) ) ? $data['status'] : '';
		if ( ! in_array( $status, self::SERVICE_STATUSES, true ) ) {
			$status = '';
		}
		$previous = (string) $order->get_meta( '_certif_ephoto_status' );

		$values = array(
			'_certif_ephoto_error'          => self::short_text( isset( $data['error'] ) ? $data['error'] : '' ),
			'_certif_ephoto_forward_status' => self::short_text( isset( $data['forward_status'] ) ? $data['forward_status'] : '', 100 ),
			'_certif_ephoto_accept_ready'   => ( isset( $data['accept_ready'] ) && true === $data['accept_ready'] ) ? 'yes' : 'no',
		);

		$changed = false;
		foreach ( $values as $meta_key => $value ) {
			if ( (string) $order->get_meta( $meta_key ) === $value ) {
				continue;
			}
			if ( '' === $value ) {
				$order->delete_meta_data( $meta_key );
			} else {
				$order->update_meta_data( $meta_key, $value );
			}
			$changed = true;
		}

		if ( '' !== $status && $status !== $previous ) {
			if ( 'accepted' === $status || 'rejected' === $status ) {
				self::apply_decision(
					$order,
					$status,
					self::short_text( isset( $data['reviewer_note'] ) ? $data['reviewer_note'] : '' ),
					'',
					self::short_text( isset( $data['decided_at'] ) ? $data['decided_at'] : '', 64 ),
					true
				);
				return; // apply_decision() saves.
			}
			$order->update_meta_data( '_certif_ephoto_status', $status );
			$changed = true;
		}

		if ( $changed ) {
			$order->save();
		}
	}

	/**
	 * Mark a dossier as missing on the service (404), unless it was decided.
	 */
	public static function mark_missing( $order ) {
		if ( ! $order instanceof WC_Order ) {
			return;
		}
		$previous = (string) $order->get_meta( '_certif_ephoto_status' );
		if ( in_array( $previous, array( 'accepted', 'rejected', 'missing' ), true ) ) {
			return;
		}
		$order->update_meta_data( '_certif_ephoto_status', 'missing' );
		$order->update_meta_data( '_certif_ephoto_accept_ready', 'no' );
		$order->save();
	}

	/**
	 * Apply an accept/reject decision to the WooCommerce order.
	 *
	 * @param WC_Order $order
	 * @param string   $status         'accepted' or 'rejected'.
	 * @param string   $reason         Rejection reason / reviewer note.
	 * @param string   $reviewer_label Display name for the note (empty when synced).
	 * @param string   $decided_at     Decision date from the service.
	 * @param bool     $from_sync      True when reconciling from the service state.
	 */
	public static function apply_decision( $order, $status, $reason = '', $reviewer_label = '', $decided_at = '', $from_sync = false ) {
		if ( ! $order instanceof WC_Order || ! in_array( $status, array( 'accepted', 'rejected' ), true ) ) {
			return;
		}

		$order->update_meta_data( '_certif_ephoto_status', $status );
		$order->update_meta_data( '_certif_ephoto_decided_at', '' !== (string) $decided_at ? (string) $decided_at : current_time( 'mysql' ) );
		$order->update_meta_data( '_certif_ephoto_accept_ready', 'no' );
		if ( '' !== (string) $reason ) {
			$order->update_meta_data( '_certif_ephoto_reason', (string) $reason );
		}
		if ( ! $from_sync ) {
			$order->update_meta_data( '_certif_ephoto_reviewer', self::reviewer_login() );
		}

		$reason_text = '' !== (string) $reason ? (string) $reason : '—';
		if ( 'accepted' === $status ) {
			$target      = (string) get_option( 'certif_ephoto_accepted_status', 'completed' );
			$status_note = __( 'Dossier ePhoto validé.', 'certif-ephoto-control' );
			$note        = $from_sync
				? __( '[Certif ID] Dossier accepté (état synchronisé depuis le service de contrôle).', 'certif-ephoto-control' )
				: sprintf( __( '[Certif ID] Dossier accepté par %s et transmis à Make / ePhoto.', 'certif-ephoto-control' ), $reviewer_label );
		} else {
			$target      = (string) get_option( 'certif_ephoto_rejected_status', 'failed' );
			$status_note = sprintf( __( 'Dossier ePhoto refusé : %s', 'certif-ephoto-control' ), $reason_text );
			$note        = $from_sync
				? sprintf( __( '[Certif ID] Dossier refusé (état synchronisé depuis le service de contrôle). Motif : %s', 'certif-ephoto-control' ), $reason_text )
				: sprintf( __( '[Certif ID] Dossier refusé par %1$s. Motif : %2$s', 'certif-ephoto-control' ), $reviewer_label, $reason_text );
		}

		if ( self::is_valid_order_status( $target ) && $order->get_status() !== $target ) {
			$order->set_status( $target, $status_note );
		}
		$order->save();
		$order->add_order_note( $note );
	}

	/**
	 * Is $slug (without "wc-") a registered order status?
	 */
	private static function is_valid_order_status( $slug ) {
		$slug = (string) $slug;
		if ( '' === $slug || 'none' === $slug || ! function_exists( 'wc_get_order_statuses' ) ) {
			return false;
		}
		return array_key_exists( 'wc-' . $slug, wc_get_order_statuses() );
	}
}
