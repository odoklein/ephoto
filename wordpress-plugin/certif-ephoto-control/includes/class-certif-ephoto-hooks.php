<?php
/**
 * WooCommerce hooks: background auto-ingest, order list column and order meta box.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Certif_Ephoto_Hooks {

	const INGEST_ACTION = 'certif_ephoto_ingest_order';
	const AS_GROUP      = 'certif-ephoto';

	public static function init() {
		// Auto-ingest (scheduled in the background, never inline in the payment request when possible).
		add_action( 'woocommerce_payment_complete', array( __CLASS__, 'on_order_paid' ), 20, 1 );
		add_action( 'woocommerce_order_status_processing', array( __CLASS__, 'on_order_paid' ), 20, 1 );
		add_action( self::INGEST_ACTION, array( __CLASS__, 'run_ingest' ), 10, 1 );

		// Orders list column (legacy post type & HPOS).
		add_filter( 'manage_edit-shop_order_columns', array( __CLASS__, 'add_order_column' ), 20 );
		add_action( 'manage_shop_order_posts_custom_column', array( __CLASS__, 'render_order_column' ), 20, 2 );
		add_filter( 'manage_woocommerce_page_wc-orders_columns', array( __CLASS__, 'add_order_column' ), 20 );
		add_action( 'manage_woocommerce_page_wc-orders_custom_column', array( __CLASS__, 'render_hpos_order_column' ), 20, 2 );

		// Order meta box.
		add_action( 'add_meta_boxes', array( __CLASS__, 'add_order_metabox' ) );
	}

	/**
	 * Is the plugin's auto-ingest the active intake path?
	 */
	public static function auto_ingest_enabled() {
		return 'yes' === get_option( 'certif_ephoto_auto_ingest', 'yes' );
	}

	/**
	 * Does the order need an automatic ingest? Live or rejected dossiers are left alone.
	 */
	private static function needs_ingest( $order ) {
		$sub_id = Certif_Ephoto_Client::clean_submission_id( $order->get_meta( '_certif_ephoto_submission_id' ) );
		if ( '' === $sub_id ) {
			return true;
		}
		return 'error' === (string) $order->get_meta( '_certif_ephoto_status' );
	}

	/**
	 * Payment complete / status processing: schedule the ingest.
	 */
	public static function on_order_paid( $order_id ) {
		if ( ! self::auto_ingest_enabled() || ! function_exists( 'wc_get_order' ) ) {
			return;
		}
		$order_id = absint( $order_id );
		$order    = $order_id ? wc_get_order( $order_id ) : false;
		if ( ! $order instanceof WC_Order || ! self::needs_ingest( $order ) ) {
			return;
		}

		if ( function_exists( 'as_enqueue_async_action' ) ) {
			$args = array( $order_id );
			if ( function_exists( 'as_has_scheduled_action' ) ) {
				if ( as_has_scheduled_action( self::INGEST_ACTION, $args, self::AS_GROUP ) ) {
					return;
				}
			} elseif ( function_exists( 'as_next_scheduled_action' ) && false !== as_next_scheduled_action( self::INGEST_ACTION, $args, self::AS_GROUP ) ) {
				return;
			}
			as_enqueue_async_action( self::INGEST_ACTION, $args, self::AS_GROUP );
			return;
		}

		// No Action Scheduler: run inline (errors are still recorded as order notes).
		self::run_ingest( $order_id );
	}

	/**
	 * Background worker (Action Scheduler) or inline fallback.
	 * The per-order lock lives in Certif_Ephoto_Client::ingest_order().
	 */
	public static function run_ingest( $order_id ) {
		$order_id = absint( $order_id );
		if ( ! $order_id || ! self::auto_ingest_enabled() || ! function_exists( 'wc_get_order' ) ) {
			return;
		}
		$order = wc_get_order( $order_id );
		if ( ! $order instanceof WC_Order || ! self::needs_ingest( $order ) ) {
			return;
		}

		$data = Certif_Ephoto_Order_Reader::extract_dossier_data( $order );
		if ( is_wp_error( $data ) || ( empty( $data['photo_url'] ) && empty( $data['signature_url'] ) ) ) {
			return; // Not an ePhoto order.
		}

		$result = Certif_Ephoto_Client::ingest_order( $order_id, false );
		if ( ! is_wp_error( $result ) || 'ingest_locked' === $result->get_error_code() ) {
			return;
		}

		$order = wc_get_order( $order_id );
		if ( $order instanceof WC_Order ) {
			$order->add_order_note(
				sprintf(
					__( '[Certif ID] Échec de l’envoi automatique au service de contrôle : %s. Utilisez « Lancer l’analyse » dans Contrôle photos après correction.', 'certif-ephoto-control' ),
					$result->get_error_message()
				)
			);
		}
	}

	/**
	 * Add column to WooCommerce orders table.
	 */
	public static function add_order_column( $columns ) {
		$new_columns = array();
		foreach ( $columns as $key => $column ) {
			$new_columns[ $key ] = $column;
			if ( 'order_status' === $key ) {
				$new_columns['certif_ephoto'] = __( 'ePhoto ANTS', 'certif-ephoto-control' );
			}
		}
		if ( ! isset( $new_columns['certif_ephoto'] ) ) {
			$new_columns['certif_ephoto'] = __( 'ePhoto ANTS', 'certif-ephoto-control' );
		}
		return $new_columns;
	}

	/**
	 * Render column for legacy post type orders.
	 */
	public static function render_order_column( $column, $post_id ) {
		if ( 'certif_ephoto' !== $column ) {
			return;
		}
		self::display_column_content( $post_id );
	}

	/**
	 * Render column for HPOS orders.
	 */
	public static function render_hpos_order_column( $column, $order ) {
		if ( 'certif_ephoto' !== $column ) {
			return;
		}
		$order_id = is_object( $order ) ? $order->get_id() : $order;
		self::display_column_content( $order_id );
	}

	/**
	 * Inline style of a status badge.
	 */
	private static function badge_style( $status ) {
		$colors = array(
			'to_send'    => array( '#fef3c7', '#92400e' ),
			'processing' => array( '#e0f2fe', '#0369a1' ),
			'pending'    => array( '#fef3c7', '#92400e' ),
			'error'      => array( '#fee2e2', '#b91c1c' ),
			'accepted'   => array( '#d1fae5', '#065f46' ),
			'rejected'   => array( '#ffe4e6', '#9f1239' ),
			'missing'    => array( '#f1f5f9', '#475569' ),
		);
		$c = isset( $colors[ $status ] ) ? $colors[ $status ] : $colors['to_send'];
		return 'background:' . $c[0] . ';color:' . $c[1] . ';padding:3px 8px;border-radius:4px;font-size:11px;font-weight:600;text-decoration:none;';
	}

	private static function display_column_content( $order_id ) {
		$order = function_exists( 'wc_get_order' ) ? wc_get_order( $order_id ) : false;
		if ( ! $order instanceof WC_Order ) {
			echo '—';
			return;
		}

		$sub_id = Certif_Ephoto_Client::clean_submission_id( $order->get_meta( '_certif_ephoto_submission_id' ) );
		$status = (string) $order->get_meta( '_certif_ephoto_status' );

		if ( '' === $sub_id ) {
			$data = Certif_Ephoto_Order_Reader::extract_dossier_data( $order );
			if ( ! is_wp_error( $data ) && ( ! empty( $data['photo_url'] ) || ! empty( $data['signature_url'] ) ) ) {
				echo '<span style="' . esc_attr( self::badge_style( 'to_send' ) ) . '">' . esc_html( Certif_Ephoto_Admin::status_label( 'to_send' ) ) . '</span>';
			} else {
				echo '<span style="color:#94a3b8;">—</span>';
			}
			return;
		}

		if ( '' === $status || ! array_key_exists( $status, Certif_Ephoto_Admin::status_labels() ) ) {
			$status = 'processing';
		}

		$label = Certif_Ephoto_Admin::status_label( $status );
		if ( current_user_can( Certif_Ephoto_Admin::capability() ) ) {
			echo '<a href="' . esc_url( Certif_Ephoto_Admin::review_url( $order->get_id() ) ) . '" style="' . esc_attr( self::badge_style( $status ) ) . '">' . esc_html( $label ) . '</a>';
		} else {
			echo '<span style="' . esc_attr( self::badge_style( $status ) ) . '">' . esc_html( $label ) . '</span>';
		}
	}

	/**
	 * Register meta box in order page.
	 */
	public static function add_order_metabox() {
		$screen = 'shop_order';
		if ( class_exists( '\Automattic\WooCommerce\Utilities\OrderUtil' )
			&& \Automattic\WooCommerce\Utilities\OrderUtil::custom_orders_table_usage_is_enabled()
			&& function_exists( 'wc_get_page_screen_id' ) ) {
			$screen = wc_get_page_screen_id( 'shop-order' );
		}

		add_meta_box(
			'certif_ephoto_order_box',
			__( 'Contrôle photo & signature (Certif ID)', 'certif-ephoto-control' ),
			array( __CLASS__, 'render_order_metabox' ),
			$screen,
			'side',
			'high'
		);
	}

	/**
	 * Render the order meta box (view only: links to the review screen, never ingests).
	 */
	public static function render_order_metabox( $post_or_order ) {
		$order = false;
		if ( $post_or_order instanceof WC_Order ) {
			$order = $post_or_order;
		} elseif ( is_object( $post_or_order ) && isset( $post_or_order->ID ) && function_exists( 'wc_get_order' ) ) {
			$order = wc_get_order( $post_or_order->ID );
		}
		if ( ! $order instanceof WC_Order ) {
			return;
		}

		$order_id   = $order->get_id();
		$sub_id     = Certif_Ephoto_Client::clean_submission_id( $order->get_meta( '_certif_ephoto_submission_id' ) );
		$status     = (string) $order->get_meta( '_certif_ephoto_status' );
		$error      = (string) $order->get_meta( '_certif_ephoto_error' );
		$can_review = current_user_can( Certif_Ephoto_Admin::capability() );
		$review_url = Certif_Ephoto_Admin::review_url( $order_id );

		echo '<div style="font-size:13px;line-height:1.6;">';
		if ( '' !== $sub_id ) {
			if ( '' === $status ) {
				$status = 'processing';
			}
			echo '<p><strong>' . esc_html__( 'Dossier ID :', 'certif-ephoto-control' ) . '</strong> <code>' . esc_html( $sub_id ) . '</code></p>';
			echo '<p><strong>' . esc_html__( 'Statut :', 'certif-ephoto-control' ) . '</strong> <span style="' . esc_attr( self::badge_style( $status ) ) . '">' . esc_html( Certif_Ephoto_Admin::status_label( $status ) ) . '</span></p>';
			if ( 'error' === $status && '' !== $error ) {
				echo '<p style="color:#b91c1c;">' . esc_html( $error ) . '</p>';
			}
			if ( $can_review ) {
				echo '<p><a href="' . esc_url( $review_url ) . '" class="button button-primary" style="width:100%;text-align:center;">' . esc_html__( 'Ouvrir le contrôle photo', 'certif-ephoto-control' ) . '</a></p>';
			}
		} else {
			$data = Certif_Ephoto_Order_Reader::extract_dossier_data( $order );
			if ( ! is_wp_error( $data ) && ! empty( $data['photo_url'] ) && ! empty( $data['signature_url'] ) ) {
				echo '<p style="color:#0369a1;">' . esc_html__( 'Photo et signature détectées, pas encore envoyées au service.', 'certif-ephoto-control' ) . '</p>';
				if ( $can_review ) {
					echo '<p><a href="' . esc_url( Certif_Ephoto_Admin::review_url( $order_id, false ) ) . '" class="button button-secondary" style="width:100%;text-align:center;">' . esc_html__( 'Voir dans Contrôle photos', 'certif-ephoto-control' ) . '</a></p>';
				}
			} else {
				echo '<p style="color:#64748b;">' . esc_html__( 'Aucune photo ou signature exploitable trouvée sur cette commande.', 'certif-ephoto-control' ) . '</p>';
			}
		}
		echo '</div>';
	}
}
