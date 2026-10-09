<?php
/**
 * WordPress privacy tools: personal data exporter / eraser for the plugin's
 * order meta (file source URLs, dossier id, decision), keyed by billing email.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Certif_Ephoto_Privacy {

	const BATCH = 20;

	public static function init() {
		add_filter( 'wp_privacy_personal_data_exporters', array( __CLASS__, 'register_exporter' ) );
		add_filter( 'wp_privacy_personal_data_erasers', array( __CLASS__, 'register_eraser' ) );
		add_action( 'admin_init', array( __CLASS__, 'add_policy_content' ) );
	}

	public static function register_exporter( $exporters ) {
		$exporters['certif-ephoto-control'] = array(
			'exporter_friendly_name' => __( 'Contrôle photo d’identité (Certif ID)', 'certif-ephoto-control' ),
			'callback'               => array( __CLASS__, 'export' ),
		);
		return $exporters;
	}

	public static function register_eraser( $erasers ) {
		$erasers['certif-ephoto-control'] = array(
			'eraser_friendly_name' => __( 'Contrôle photo d’identité (Certif ID)', 'certif-ephoto-control' ),
			'callback'             => array( __CLASS__, 'erase' ),
		);
		return $erasers;
	}

	/**
	 * Exported meta keys and their labels.
	 */
	private static function exported_fields() {
		return array(
			'_certif_ephoto_submission_id' => __( 'Identifiant du dossier de contrôle', 'certif-ephoto-control' ),
			'_certif_ephoto_status'        => __( 'Statut du contrôle', 'certif-ephoto-control' ),
			'_certif_ephoto_photo_url'     => __( 'URL de la photo envoyée', 'certif-ephoto-control' ),
			'_certif_ephoto_signature_url' => __( 'URL de la signature envoyée', 'certif-ephoto-control' ),
			'_certif_ephoto_ingested_at'   => __( 'Date d’envoi au contrôle', 'certif-ephoto-control' ),
			'_certif_ephoto_decided_at'    => __( 'Date de décision', 'certif-ephoto-control' ),
			'_certif_ephoto_reason'        => __( 'Motif / note du contrôleur', 'certif-ephoto-control' ),
			'_certif_ephoto_error'         => __( 'Erreur de traitement', 'certif-ephoto-control' ),
		);
	}

	/**
	 * Orders of a billing email, one page at a time.
	 *
	 * @return WC_Order[]
	 */
	private static function orders_for_email( $email, $page ) {
		if ( ! function_exists( 'wc_get_orders' ) || ! is_email( $email ) ) {
			return array();
		}
		$orders = wc_get_orders(
			array(
				'billing_email' => $email,
				'type'          => 'shop_order',
				'limit'         => self::BATCH,
				'page'          => max( 1, (int) $page ),
				'orderby'       => 'ID',
				'order'         => 'ASC',
				'return'        => 'objects',
			)
		);
		return is_array( $orders ) ? $orders : array();
	}

	/**
	 * Personal data exporter callback.
	 */
	public static function export( $email_address, $page = 1 ) {
		$orders = self::orders_for_email( $email_address, $page );
		$items  = array();

		foreach ( $orders as $order ) {
			if ( ! $order instanceof WC_Order ) {
				continue;
			}
			$data = array();
			foreach ( self::exported_fields() as $meta_key => $label ) {
				$value = $order->get_meta( $meta_key );
				if ( is_scalar( $value ) && '' !== (string) $value ) {
					$data[] = array(
						'name'  => $label,
						'value' => (string) $value,
					);
				}
			}
			if ( empty( $data ) ) {
				continue;
			}
			array_unshift(
				$data,
				array(
					'name'  => __( 'Commande', 'certif-ephoto-control' ),
					'value' => (string) $order->get_order_number(),
				)
			);
			$items[] = array(
				'group_id'          => 'certif-ephoto',
				'group_label'       => __( 'Contrôle photo d’identité', 'certif-ephoto-control' ),
				'group_description' => __( 'Références au contrôle de la photo d’identité et de la signature associées à vos commandes.', 'certif-ephoto-control' ),
				'item_id'           => 'certif-ephoto-order-' . $order->get_id(),
				'data'              => $data,
			);
		}

		return array(
			'data' => $items,
			'done' => count( $orders ) < self::BATCH,
		);
	}

	/**
	 * Personal data eraser callback: removes the plugin's order meta.
	 * Copies held by the control service are purged by the service 30 days after decision.
	 */
	public static function erase( $email_address, $page = 1 ) {
		$orders  = self::orders_for_email( $email_address, $page );
		$removed = false;

		foreach ( $orders as $order ) {
			if ( ! $order instanceof WC_Order ) {
				continue;
			}
			$changed = false;
			foreach ( Certif_Ephoto_Client::meta_keys() as $meta_key ) {
				if ( '' !== $order->get_meta( $meta_key ) ) {
					$order->delete_meta_data( $meta_key );
					$changed = true;
				}
			}
			if ( $changed ) {
				$order->save();
				$removed = true;
			}
		}

		$messages = array();
		if ( $removed ) {
			$messages[] = __( 'Certif ID : références au contrôle photo supprimées des commandes. Les fichiers détenus par le service de contrôle sont purgés automatiquement 30 jours après la décision ; les dossiers non décidés doivent être refusés pour être purgés.', 'certif-ephoto-control' );
		}

		return array(
			'items_removed'  => $removed,
			'items_retained' => false,
			'messages'       => $messages,
			'done'           => count( $orders ) < self::BATCH,
		);
	}

	/**
	 * Suggested privacy policy text.
	 */
	public static function add_policy_content() {
		if ( ! function_exists( 'wp_add_privacy_policy_content' ) ) {
			return;
		}
		$content = '<p>' . esc_html__( 'Lorsque vous commandez une photo d’identité, la photo et la signature que vous téléversez sont transmises à notre service de contrôle (conformité ANTS) puis, après validation par un opérateur, à notre partenaire ePhoto. Les fichiers sont conservés par le service de contrôle au plus 30 jours après la décision. La commande conserve l’adresse des fichiers téléversés, l’identifiant du dossier de contrôle et la décision.', 'certif-ephoto-control' ) . '</p>';
		wp_add_privacy_policy_content( 'Certif ID – Contrôle photos', wp_kses_post( $content ) );
	}
}
