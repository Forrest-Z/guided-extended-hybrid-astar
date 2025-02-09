//
// Created by Schumann on 23.06.22.
//
#ifndef FREESPACE_PLANNER_A_STAR_HPP
#define FREESPACE_PLANNER_A_STAR_HPP

#include <iostream>
#include <map>
#include <cmath>
#include <unordered_map>
#include "opencv2/imgproc.hpp"

#include "cuda_lib/max_pool.hpp"

#include "collision_checker_lib/collision_checking.hpp"

#include "util_lib/data_structures2.hpp"
#include "util_lib/util2.hpp"
#include "util_lib/transforms.hpp"

#include "deps_lib/VoronoiDiagramGenerator.hpp"
#include "deps_lib/nanoflann.hpp"
#include "deps_lib/KdTreeVectorOfVectorsAdaptors.hpp"

// #include "logger_setup.hpp"

#include "yaml-cpp/yaml.h"

namespace py = pybind11;

/**
 * Main class, that organizes the 2D search
 */
class AStar
{
private:
  inline static constexpr uint8_t NB_GRID_MOTIONS = 8;
  inline static constexpr size_t NUM_RESULTS = 1;
  inline static constexpr uint8_t MAP_DIM = 2;
  inline static constexpr size_t VOR_DIM = 150;  // 300 for paper cases
  inline static constexpr uint8_t VOR_PAD = 20;
  inline static constexpr uint8_t VOR_DIM_SAMPLING = VOR_DIM + VOR_PAD + VOR_PAD;

  // Data for the Voronoi potential field
  using vector_of_arrays = std::vector<std::array<double, MAP_DIM>>;

  // Dimension set at compile-time
  using my_kd_tree_t = KDTreeVectorOfVectorsAdaptor<vector_of_arrays, double, MAP_DIM, nanoflann::metric_L2_Simple>;

  inline static Point<double> patch_origin_utm_;
  inline static Point<int> patch_origin_astar_;
  inline static vector_of_arrays obs_samples_;
  inline static vector_of_arrays vor_samples_;

  inline static double unknown_cost_w_;
  inline static double astar_res_;
  inline static size_t patch_dim_;
  inline static double gm_res_;
  inline static bool heuristic_early_exit_;
  inline static unsigned int max_extra_nodes_;
  inline static size_t obs_size_ = 1000;
  inline static size_t vor_size_ = 1000;

  inline static double motion_res_min_;
  inline static double motion_res_max_;
  inline static double dist_val_min_;
  inline static double dist_val_max_;

  inline static std::array<std::array<double, MAP_DIM>, VOR_DIM * VOR_DIM> vor_coords_;
  inline static std::array<std::array<double, MAP_DIM>, VOR_DIM_SAMPLING * VOR_DIM_SAMPLING> vor_coords_sampling_;

  inline static const std::array<Point<int>, NB_GRID_MOTIONS> motion_ = {
    { { 1, 0 }, { 0, 1 }, { -1, 0 }, { 0, -1 }, { -1, -1 }, { -1, 1 }, { 1, -1 }, { 1, 1 } }
  };
  inline static std::array<double, NB_GRID_MOTIONS> movement_distances_;

public:
  // voronoi field
  inline static double alpha_;
  inline static double do_max_;
  inline static double do_min_;

  inline static double astar_movement_cost_;
  inline static double astar_prox_cost_;
  inline static double astar_lane_movement_cost_;

  inline static int astar_dim_;
  inline static std::string path2config_;

  inline static std::unordered_map<size_t, NodeDisc> closed_set_path_;
  inline static std::unordered_map<size_t, NodeDisc> closed_set_guidance_;

  inline static std::vector<std::pair<double, NodeDisc>> nodes_near_goal_;

  inline static Vec2DFlat<uint8_t> astar_grid_;
  inline static Vec2DFlat<double> movement_cost_map_;

  // voronoi dependant arrays are copied on new patch creation
  inline static Vec2DFlat<double> h_prox_arr_;
  inline static Vec2DFlat<double> temp_h_prox_arr_;
  inline static Vec2DFlat<double> motion_res_map_;
  inline static Vec2DFlat<double> temp_motion_res_map_;
  inline static Vec2DFlat<double> obs_x_grad_;
  inline static Vec2DFlat<double> temp_obs_x_grad_;
  inline static Vec2DFlat<double> obs_y_grad_;
  inline static Vec2DFlat<double> temp_obs_y_grad_;

  inline static std::vector<size_t> path_indices_;

  static void initialize(int patch_dim, const Point<double>& patch_origin_utm, const std::string& path2config);

  static void init_structs(int patch_dim);

  static void reinit(const Point<double>& patch_origin_utm, int patch_dim);

  static std::pair<std::vector<int>, std::vector<int>> getAstarPath(int x_ind, int y_ind);

  static py::array_t<double> getObsGradX();

  static py::array_t<double> getObsGradY();

  static void calcDistanceHeuristic(const Point<int>& goal_pos,
                                    const Point<int>& start_pos,
                                    bool for_path = true,
                                    bool get_only_near = false);

  static std::unordered_map<size_t, NodeDisc> getDistanceHeuristic(bool for_path = false);

  static void calcVoronoiPotentialField(const Point<int>& ego_index);

  static std::pair<size_t, size_t> reverse2DIndex(size_t idx);

  static void calcAstarGridCuda();

  //  static void calcAstarGrid();

  static size_t calcIndex(size_t x_ind, size_t y_ind);

  static size_t calcIndex(const NodeDisc& node);

  static void resetMovementMap();

  static void setMovementMap(const LaneGraph::edges_t& edges);

private:
  static int findValidNeighborIndex(int start_idx, std::unordered_map<size_t, NodeDisc> heuristic);

  static void calcVorFieldElement(const std::array<double, 2>& query_vec,
                                  const my_kd_tree_t& obs_mat_index,
                                  const my_kd_tree_t& vor_mat_index);

  static std::array<double, NB_GRID_MOTIONS> getMovementDists();

  static bool verifyNode(int x_ind, int y_ind);

  static Point<int> getCurrentMapOrigin(const Point<int>& ego_pos, size_t dim);
};

#endif  // FREESPACE_PLANNER_A_STAR_HPP
