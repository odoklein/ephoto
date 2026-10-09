<?php
/**
 * Order reader for Certif ID Ephoto.
 * Reads customer info and extracts photo & signature URLs from WooCommerce orders,
 * including ThemeComplete Extra Product Options (TM EPO) and standard upload fields.
 *
 * Security: only image URLs hosted on THIS site, under the uploads directory, are
 * accepted. Numeric values are treated as attachment ids only for the explicit
 * override meta keys and TM EPO upload fields, and must be image attachments.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Certif_Ephoto_Order_Reader {

	/**
	 * Extensions accepted by the service.
	 */
	const IMAGE_PATH_REGEX = '/\.(jpe?g|png|webp|bmp|tiff?)$/i';

	/**
	 * Extract all ePhoto dossier data from a WooCommerce order.
	 *
	 * @param int|WC_Order $order_or_id
	 * @return array|WP_Error Array with order_id, order_number, customer, photo_url, signature_url, or WP_Error.
	 */
	public static function extract_dossier_data( $order_or_id ) {
		$order = $order_or_id;
		if ( is_numeric( $order_or_id ) && function_exists( 'wc_get_order' ) ) {
			$order = wc_get_order( absint( $order_or_id ) );
		}
		if ( ! $order instanceof WC_Order ) {
			return new WP_Error( 'invalid_order', __( 'Commande WooCommerce introuvable.', 'certif-ephoto-control' ) );
		}

		$order_id = $order->get_id();

		// Customer information.
		$first_name = trim( (string) $order->get_billing_first_name() );
		$last_name  = trim( (string) $order->get_billing_last_name() );
		$full_name  = trim( $first_name . ' ' . $last_name );
		if ( '' === $full_name ) {
			$full_name = trim( (string) $order->get_formatted_billing_full_name() );
		}
		if ( '' === $full_name ) {
			$full_name = 'Client #' . $order->get_order_number();
		}
		$email = trim( (string) $order->get_billing_email() );

		$photo_url     = '';
		$signature_url = '';
		$items         = $order->get_items();

		// 1. Explicit override meta keys from the settings (item level, then order level).
		$custom_photo_key = trim( (string) get_option( 'certif_ephoto_photo_meta_key', '' ) );
		$custom_sign_key  = trim( (string) get_option( 'certif_ephoto_sign_meta_key', '' ) );
		if ( '' !== $custom_photo_key ) {
			$photo_url = self::find_explicit( $order, $items, $custom_photo_key );
		}
		if ( '' !== $custom_sign_key ) {
			$signature_url = self::find_explicit( $order, $items, $custom_sign_key );
		}

		// 2. Heuristics on order items.
		if ( '' === $photo_url || '' === $signature_url ) {
			foreach ( $items as $item ) {
				// A. TM Extra Product Options (_tmcartepo_data).
				$tm_data = $item->get_meta( '_tmcartepo_data' );
				if ( ! empty( $tm_data ) ) {
					$tm_data = maybe_unserialize( $tm_data );
					if ( is_array( $tm_data ) ) {
						foreach ( $tm_data as $field ) {
							if ( ! is_array( $field ) ) {
								continue;
							}
							$label = isset( $field['name'] ) && is_scalar( $field['name'] ) ? (string) $field['name'] : '';
							if ( '' === $label && isset( $field['section_label'] ) && is_scalar( $field['section_label'] ) ) {
								$label = (string) $field['section_label'];
							}
							$val = '';
							if ( isset( $field['value'] ) ) {
								$val = $field['value'];
							} elseif ( isset( $field['display'] ) ) {
								$val = $field['display'];
							}
							$is_upload_field = isset( $field['element'] ) && is_array( $field['element'] )
								&& isset( $field['element']['type'] ) && 'upload' === $field['element']['type'];

							$url = self::clean_url( $val, $is_upload_field );
							if ( '' === $url ) {
								continue;
							}
							if ( '' === $photo_url && self::is_photo_field( $label, $url ) ) {
								$photo_url = $url;
							} elseif ( '' === $signature_url && self::is_signature_field( $label, $url ) ) {
								$signature_url = $url;
							}
						}
					}
				}

				// B. Standard item meta (URLs, or image attachment IDs if key matches).
				foreach ( $item->get_meta_data() as $meta ) {
					$key = (string) $meta->key;
					if ( '_tmcartepo_data' === $key ) {
						continue;
					}
					$is_candidate = self::is_photo_field( $key, '' ) || self::is_signature_field( $key, '' );
					$url = self::clean_url( $meta->value, $is_candidate );
					if ( '' === $url ) {
						continue;
					}
					if ( '' === $photo_url && self::is_photo_field( $key, $url ) ) {
						$photo_url = $url;
					} elseif ( '' === $signature_url && self::is_signature_field( $key, $url ) ) {
						$signature_url = $url;
					}
				}

				if ( '' !== $photo_url && '' !== $signature_url ) {
					break;
				}
			}
		}

		// 3. Fallback: order-level meta (URLs or attachment IDs for candidate keys).
		if ( '' === $photo_url || '' === $signature_url ) {
			foreach ( $order->get_meta_data() as $meta ) {
				$key = (string) $meta->key;
				if ( 0 === strpos( $key, '_certif_ephoto_' ) ) {
					continue;
				}
				$is_candidate = self::is_photo_field( $key, '' ) || self::is_signature_field( $key, '' );
				$url = self::clean_url( $meta->value, $is_candidate );
				if ( '' === $url ) {
					continue;
				}
				if ( '' === $photo_url && self::is_photo_field( $key, $url ) ) {
					$photo_url = $url;
				} elseif ( '' === $signature_url && self::is_signature_field( $key, $url ) ) {
					$signature_url = $url;
				}
			}
		}

		return array(
			'order_id'      => $order_id,
			'order_number'  => (string) $order->get_order_number(),
			'customer'      => array(
				'name'  => $full_name,
				'email' => $email,
			),
			'photo_url'     => $photo_url,
			'signature_url' => $signature_url,
			'order_status'  => $order->get_status(),
			'date_created'  => $order->get_date_created() ? $order->get_date_created()->format( 'Y-m-d H:i:s' ) : '',
		);
	}

	/**
	 * Look up an explicit override meta key: items first, then the order.
	 * Attachment ids are allowed here (must be image attachments).
	 */
	private static function find_explicit( $order, $items, $meta_key ) {
		foreach ( $items as $item ) {
			$val = $item->get_meta( $meta_key, true );
			if ( null !== $val && '' !== $val ) {
				$url = self::clean_url( $val, true );
				if ( '' !== $url ) {
					return $url;
				}
			}
		}
		$val = $order->get_meta( $meta_key, true );
		if ( null !== $val && '' !== $val ) {
			return self::clean_url( $val, true );
		}
		return '';
	}

	/**
	 * Check if a field represents a photo.
	 */
	private static function is_photo_field( $key_or_label, $url ) {
		$text = mb_strtolower( (string) $key_or_label, 'UTF-8' );
		if ( preg_match( '/\b(photo|visage|portrait|identit|face|selfie)\b/i', $text ) ) {
			return true;
		}
		// Fallback on the file name (the URL was already validated as a local upload).
		$filename = mb_strtolower( basename( (string) wp_parse_url( $url, PHP_URL_PATH ) ), 'UTF-8' );
		return (bool) preg_match( '/\b(photo|visage|portrait|id)\b/i', $filename );
	}

	/**
	 * Check if a field represents a signature.
	 */
	private static function is_signature_field( $key_or_label, $url ) {
		$text = mb_strtolower( (string) $key_or_label, 'UTF-8' );
		if ( preg_match( '/\b(sign|signature)\b/i', $text ) ) {
			return true;
		}
		$filename = mb_strtolower( basename( (string) wp_parse_url( $url, PHP_URL_PATH ) ), 'UTF-8' );
		return (bool) preg_match( '/\b(sign|signature)\b/i', $filename );
	}

	/**
	 * Normalize a meta value into an absolute URL of an image in this site's uploads.
	 *
	 * @param mixed $val              Meta value.
	 * @param bool  $allow_attachment Treat a numeric value as an attachment id.
	 * @return string URL or '' when the value is not an acceptable local image.
	 */
	public static function clean_url( $val, $allow_attachment = false ) {
		if ( is_array( $val ) ) {
			foreach ( array( 'url', 'value', 'file' ) as $sub_key ) {
				if ( isset( $val[ $sub_key ] ) ) {
					return self::clean_url( $val[ $sub_key ], $allow_attachment );
				}
			}
			return '';
		}

		if ( is_int( $val ) || ( is_string( $val ) && preg_match( '/^\s*\d+\s*$/', $val ) ) ) {
			if ( ! $allow_attachment ) {
				return '';
			}
			$attachment_id = absint( $val );
			if ( $attachment_id <= 0 || ! wp_attachment_is_image( $attachment_id ) ) {
				return '';
			}
			$attachment_url = wp_get_attachment_url( $attachment_id );
			return $attachment_url ? self::normalize_upload_url( $attachment_url ) : '';
		}

		if ( ! is_string( $val ) ) {
			return '';
		}

		$val = trim( $val );
		if ( '' === $val ) {
			return '';
		}

		// HTML link or image, e.g. <a href="https://...">file.jpg</a>.
		if ( preg_match( '/\b(?:href|src)\s*=\s*[\'"]([^\'"]+)[\'"]/i', $val, $match ) ) {
			$val = trim( html_entity_decode( $match[1], ENT_QUOTES, 'UTF-8' ) );
		} elseif ( false !== strpos( $val, '<' ) ) {
			return '';
		}

		return self::normalize_upload_url( $val );
	}

	/**
	 * Resolve relative forms and validate the result with is_allowed_upload_url().
	 */
	private static function normalize_upload_url( $val ) {
		$val = trim( (string) $val );
		if ( '' === $val || false !== strpos( $val, '\\' ) ) {
			return '';
		}

		// The fragment is never sent to a server; drop it.
		$hash = strpos( $val, '#' );
		if ( false !== $hash ) {
			$val = substr( $val, 0, $hash );
		}

		if ( 0 === strpos( $val, '//' ) ) {
			$val = ( is_ssl() ? 'https:' : 'http:' ) . $val;
		} elseif ( 0 === strpos( $val, '/' ) ) {
			$origin = self::origin_of( home_url() );
			if ( '' === $origin ) {
				return '';
			}
			$val = $origin . $val;
		} elseif ( ! preg_match( '#^https?://#i', $val ) ) {
			if ( preg_match( '#^[a-z][a-z0-9+.\-]*:#i', $val ) ) {
				return ''; // Other schemes (file:, data:, ftp:, javascript:...).
			}
			$uploads = wp_upload_dir( null, false );
			if ( empty( $uploads['baseurl'] ) ) {
				return '';
			}
			$val = trailingslashit( $uploads['baseurl'] ) . ltrim( $val, '/' );
		}

		return self::is_allowed_upload_url( $val ) ? $val : '';
	}

	/**
	 * scheme://host[:port] of a URL.
	 */
	private static function origin_of( $url ) {
		$parts = wp_parse_url( $url );
		if ( ! is_array( $parts ) || empty( $parts['scheme'] ) || empty( $parts['host'] ) ) {
			return '';
		}
		return strtolower( $parts['scheme'] ) . '://' . $parts['host'] . ( isset( $parts['port'] ) ? ':' . (int) $parts['port'] : '' );
	}

	/**
	 * Host without a leading "www.".
	 */
	private static function bare_host( $host ) {
		$host = strtolower( trim( (string) $host, '[]' ) );
		return 0 === strpos( $host, 'www.' ) ? substr( $host, 4 ) : $host;
	}

	/**
	 * Only accept http(s) URLs on this site's host (home, site or uploads host),
	 * whose decoded PATH is under the uploads base path and ends with an image
	 * extension. Query strings are allowed but ignored for the extension test.
	 *
	 * @param string $url
	 * @return bool
	 */
	public static function is_allowed_upload_url( $url ) {
		$parts = wp_parse_url( (string) $url );
		if ( ! is_array( $parts ) || empty( $parts['scheme'] ) || empty( $parts['host'] ) || empty( $parts['path'] ) ) {
			return false;
		}
		$scheme = strtolower( $parts['scheme'] );
		if ( 'http' !== $scheme && 'https' !== $scheme ) {
			return false;
		}
		if ( isset( $parts['user'] ) || isset( $parts['pass'] ) || isset( $parts['fragment'] ) ) {
			return false;
		}

		$uploads = wp_upload_dir( null, false );
		$baseurl = isset( $uploads['baseurl'] ) ? (string) $uploads['baseurl'] : '';
		if ( '' === $baseurl ) {
			return false;
		}

		// Host check.
		$host = self::bare_host( $parts['host'] );
		$port = isset( $parts['port'] ) ? (int) $parts['port'] : 0;
		$host_ok = false;
		foreach ( array( home_url(), site_url(), $baseurl ) as $reference ) {
			$ref = wp_parse_url( $reference );
			if ( ! is_array( $ref ) || empty( $ref['host'] ) || self::bare_host( $ref['host'] ) !== $host ) {
				continue;
			}
			$ref_port = isset( $ref['port'] ) ? (int) $ref['port'] : 0;
			if ( 0 === $port || $port === $ref_port || ( 0 === $ref_port && ( 80 === $port || 443 === $port ) ) ) {
				$host_ok = true;
				break;
			}
		}
		if ( ! $host_ok ) {
			return false;
		}

		// Path check (decoded, no traversal, under uploads, image extension).
		$path = rawurldecode( $parts['path'] );
		if ( false !== strpos( $path, '\\' ) || false !== strpos( $path, "\0" ) ) {
			return false;
		}
		if ( preg_match( '#(^|/)\.{1,2}(/|$)#', $path ) ) {
			return false;
		}

		$base_path = (string) wp_parse_url( $baseurl, PHP_URL_PATH );
		$base_path = trim( rawurldecode( $base_path ), '/' );
		$base_path = '' === $base_path ? '/' : '/' . $base_path . '/';
		if ( 0 !== strpos( $path, $base_path ) ) {
			return false;
		}

		$ok = (bool) preg_match( self::IMAGE_PATH_REGEX, $path );

		/**
		 * Filter whether a customer file URL may be sent to the control service.
		 *
		 * @param bool   $ok  Result of the built-in checks.
		 * @param string $url URL being checked.
		 */
		return (bool) apply_filters( 'certif_ephoto_is_allowed_file_url', $ok, $url );
	}

	/**
	 * Diagnostic tool: inspect all items and metadata in an order.
	 *
	 * @param int $order_id
	 * @return array
	 */
	public static function inspect_order( $order_id ) {
		$order = function_exists( 'wc_get_order' ) ? wc_get_order( absint( $order_id ) ) : false;
		if ( ! $order instanceof WC_Order ) {
			return array( 'error' => __( 'Commande non trouvée.', 'certif-ephoto-control' ) );
		}

		$extraction = self::extract_dossier_data( $order );

		$items_inspect = array();
		foreach ( $order->get_items() as $item_id => $item ) {
			$meta_list = array();
			foreach ( $item->get_meta_data() as $m ) {
				$val = $m->value;
				if ( is_array( $val ) || is_object( $val ) ) {
					$val = wp_json_encode( $val );
				}
				$meta_list[ $m->key ] = $val;
			}

			$tm_data         = $item->get_meta( '_tmcartepo_data' );
			$tm_unserialized = ! empty( $tm_data ) ? maybe_unserialize( $tm_data ) : null;

			$items_inspect[] = array(
				'item_id'         => $item_id,
				'product_name'    => $item->get_name(),
				'meta_data'       => $meta_list,
				'_tmcartepo_data' => $tm_unserialized,
			);
		}

		$order_meta = array();
		foreach ( $order->get_meta_data() as $m ) {
			$val = $m->value;
			if ( is_array( $val ) || is_object( $val ) ) {
				$val = wp_json_encode( $val );
			}
			$order_meta[ $m->key ] = $val;
		}

		return array(
			'order_id'         => $order->get_id(),
			'order_status'     => $order->get_status(),
			'extracted_data'   => $extraction,
			'items'            => $items_inspect,
			'order_level_meta' => $order_meta,
		);
	}
}
